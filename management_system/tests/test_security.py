"""보안 기능 테스트: 인증·잠금·2FA·CSRF·권한·암호화·보안헤더·PDF/NAS."""
import os
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import auth

ORIGIN = {"origin": "http://testserver"}


def _login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, headers=ORIGIN, follow_redirects=False)


def _csrf(c, url="/account"):
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get(url).text).group(1)


@pytest.fixture(scope="module")
def anon():
    with TestClient(app) as c:
        for name, role in (("sec_hr", "hr"), ("sec_view", "viewer"), ("sec_lock", "hr"), ("sec_2fa", "hr")):
            if not auth.query_one("SELECT 1 FROM users WHERE username=?", (name,)):
                auth.create_user(name, name, role, "Strong-Pass-123!", must_change=False)
        yield c


def test_requires_login(anon):
    c = TestClient(app)
    r = c.get("/hr/employees", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/hr/api/salaries/bulk-preview?year=2026&rate=1").status_code == 401
    assert "swagger" not in c.get("/docs").text.lower()   # API 문서 비공개
    assert c.get("/openapi.json", follow_redirects=False).status_code in (303, 401, 404)


def test_initial_admin_forced_password_change(anon):
    c = TestClient(app)
    r = _login(c, "admin", "Init-Admin-Pw1!")
    assert r.status_code == 303
    r = c.get("/hr/dashboard", follow_redirects=False)
    assert r.headers["location"] == "/account/password"


def test_wrong_password_and_lockout(anon):
    c = TestClient(app)
    for _ in range(settings.max_failed_logins):
        r = _login(c, "sec_lock", "wrong-password")
    assert r.status_code == 401 and "잠겼" in r.text
    assert _login(c, "sec_lock", "Strong-Pass-123!").status_code == 401   # 올바른 비번도 잠금 중 거부
    assert _login(c, "nobody", "x").status_code == 401


def test_csrf_and_origin(anon):
    c = TestClient(app)
    _login(c, "sec_hr", "Strong-Pass-123!")
    data = {"employee_id": 1, "kind": "포상", "category": "표창", "action_date": "2026-01-01", "reason": "x"}
    assert c.post("/hr/rewards/save", data=data, follow_redirects=False).status_code == 403        # Origin 없음
    assert c.post("/hr/rewards/save", data=data, headers={"origin": "http://evil.com"}).status_code == 403
    assert c.post("/hr/rewards/save", data=data, headers=ORIGIN).status_code == 403                # 토큰 없음
    tok = _csrf(c)
    r = c.post("/hr/rewards/save", data={**data, "csrf_token": tok}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303


def test_viewer_restrictions(anon):
    c = TestClient(app)
    _login(c, "sec_view", "Strong-Pass-123!")
    assert c.get("/hr/dashboard").status_code == 200
    assert c.get("/hr/employees").status_code == 200
    for url in ("/hr/salaries", "/hr/contracts", "/hr/documents/1", "/hr/rewards", "/hr/specials",
                "/hr/employees/export.csv", "/admin/users", "/settings"):
        assert c.get(url).status_code == 403, url
    page = c.get("/hr/employees/1").text
    assert "🔒" in page and "010-" not in page and "현재 연봉" not in page
    dash = c.get("/hr/dashboard").text
    assert "salary_trend\": []" in dash or "\"salary_trend\":[]" in dash.replace(" ", "")
    tok = _csrf(c)
    r = c.post("/hr/rewards/save", data={"csrf_token": tok}, headers=ORIGIN)
    assert r.status_code == 403


def test_hr_cannot_admin(anon):
    c = TestClient(app)
    _login(c, "sec_hr", "Strong-Pass-123!")
    assert c.get("/admin/users").status_code == 403
    assert c.get("/admin/audit").status_code == 403


def test_totp_flow(anon):
    u = auth.query_one("SELECT * FROM users WHERE username='sec_2fa'")
    secret = auth.new_totp_secret()
    auth.enable_totp(u["id"], secret)
    c = TestClient(app)
    r = _login(c, "sec_2fa", "Strong-Pass-123!")
    assert r.headers["location"].startswith("/login/2fa")
    assert c.get("/hr/dashboard", follow_redirects=False).headers["location"] == "/login/2fa"
    tok = _csrf(c, "/login/2fa")
    assert c.post("/login/2fa", data={"code": "000000", "csrf_token": tok}, headers=ORIGIN).status_code == 401
    import time
    code = auth._hotp(secret, int(time.time() // 30))
    r = c.post("/login/2fa", data={"code": code, "csrf_token": tok, "next": "/hr/dashboard"}, headers=ORIGIN,
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/hr/dashboard"
    assert c.get("/hr/dashboard").status_code == 200
    # 같은 코드 재사용 불가
    u = auth.query_one("SELECT * FROM users WHERE username='sec_2fa'")
    assert not auth.verify_totp(u, code)


def test_fields_encrypted_at_rest(anon):
    conn = sqlite3.connect(settings.db_path)
    phone, = conn.execute("SELECT phone FROM employees WHERE phone IS NOT NULL LIMIT 1").fetchone()
    sal, = conn.execute("SELECT annual_salary FROM salaries LIMIT 1").fetchone()
    content, = conn.execute("SELECT content FROM hr_documents LIMIT 1").fetchone()
    pw, = conn.execute("SELECT password_hash FROM users LIMIT 1").fetchone()
    conn.close()
    assert phone.startswith("enc:v1:") and sal.startswith("enc:v1:") and content.startswith("enc:v1:")
    assert pw.startswith("$argon2id$")
    tok, = sqlite3.connect(settings.db_path).execute("SELECT token_hash FROM sessions LIMIT 1").fetchone()
    assert len(tok) == 64  # 세션 토큰은 해시만 저장


def test_security_headers_and_cookie(anon):
    c = TestClient(app)
    r = _login(c, "sec_hr", "Strong-Pass-123!")
    ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck and "bms_session=" in ck
    h = c.get("/hr/dashboard").headers
    assert "nonce-" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store"
    assert "server" not in h


def test_audit_records_access(anon):
    rows = auth.query("SELECT * FROM audit_logs WHERE action='인사기록 열람' AND username='sec_view'")
    assert rows and rows[0]["ip"]
    # 감사로그에 연봉 금액이 남지 않음
    assert not auth.query("SELECT 1 FROM audit_logs WHERE detail LIKE '%000000%'")


@pytest.mark.skipif(not os.environ.get("BMS_PDF_BROWSER_PATH"), reason="PDF 브라우저 없음")
def test_pdf_to_nas(anon):
    c = TestClient(app)
    _login(c, "sec_hr", "Strong-Pass-123!")
    tok = _csrf(c)
    r = c.post("/hr/documents/1/pdf", data={"csrf_token": tok}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    from app.modules.hr import repo
    d = repo.get_document(1)
    assert d["pdf_path"] and os.path.exists(d["pdf_path"]) and "BMS_인사" in d["pdf_path"]
    assert d["pdf_path"].startswith(settings.output_root)
    r = c.get("/hr/documents/1/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    with open(d["pdf_path"], "ab") as f:  # 변조 감지
        f.write(b"x")
    assert c.get("/hr/documents/1/pdf").status_code == 409
