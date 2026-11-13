"""2단계 인증: 메일 인증번호(기본), 인증 앱 대체, 이 PC 30일 기억, 관리자 설정."""
import re
import sqlite3
import os

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.db import execute, get_settings, set_setting
from app.main import app
from app.security import auth, mfa

O = {"origin": "http://testserver"}
PW = "Mf-Strong-Pass-1!"


def _raw(sql, args=()):
    con = sqlite3.connect(os.environ["BMS_DB_PATH"])
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _csrf(c, path):
    return re.search(r'name="csrf-token" content="([^"]*)"', c.get(path).text).group(1)


@pytest.fixture(scope="module", autouse=True)
def _started():
    with TestClient(app):          # DB 준비
        yield


@pytest.fixture()
def mail(monkeypatch):
    sent = []
    keys = ("mail_host", "mail_user", "mfa_method")
    before = {k: get_settings().get(k) for k in keys}
    set_setting("mail_host", "smtp.example.test")
    set_setting("mail_user", "bms@example.test")
    set_setting("mfa_method", "email")
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, attachments=None, **kw: sent.append((to, subject, body, kw)))
    yield sent
    for k, v in before.items():
        set_setting(k, v or "")


def _user(name, email=None, must_change=False):
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (name,)):
        auth.create_user(name, name, "hr", PW, must_change=must_change)
    uid = auth.query_one("SELECT id FROM users WHERE username=?", (name,))["id"]
    execute("UPDATE users SET email=?, totp_enabled=0, totp_secret=NULL, failed_count=0, locked_until=NULL WHERE id=?", (email, uid))
    execute("DELETE FROM mfa_devices WHERE user_id=?", (uid,))
    return uid


def _login(c, name):
    return c.post("/login", data={"username": name, "password": PW, "next": "/hr/dashboard"}, headers=O, follow_redirects=False)


def _code(sent):
    return re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)


def test_code_mail_format(mail):
    """제목에 번호(폰 알림에서 바로 보임) + [BMS시스템], 본문은 CPO 형식(HTML + 일반 텍스트)."""
    uid = _user("mf_fmt", "mf.fmt@example.test")
    with TestClient(app) as c:
        _login(c, "mf_fmt")
    execute("DELETE FROM sessions WHERE user_id=?", (uid,))     # 끝내지 않은 인증 세션 정리(다른 시험에 영향 없게)
    to, subject, body, kw = mail[-1]
    code = _code(mail)
    assert subject == f"[BMS시스템] 로그인 인증번호 {code}"
    assert kw["from_name"] == "BMS시스템" and kw["plain"] is True
    assert body.startswith("mf_fmt님, 로그인 인증번호는") and "5분 안에 입력해 주세요" in body and "접속 위치:" in body
    h = kw["html"]
    assert f">{code}</p>" in h and "font-weight:700" in h and "letter-spacing" in h
    assert "#c62828" in h and "본인이 로그인한 것이 아니면 비밀번호를 바꾸고 관리자에게 알려 주세요." in h
    assert h.index("로그인 인증번호는") < h.index(code) < h.index("입니다.") < h.index("본인이") < h.index("접속 위치")


def test_mailer_html_plain(monkeypatch):
    import smtplib
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg

    keys = ("mail_host", "mail_user", "mail_port", "mail_cc", "mail_signature", "mail_from_name")
    before = {k: get_settings().get(k) for k in keys}
    for k, v in zip(keys, ("smtp.example.test", "bms@example.test", "465", "cc@example.test", "서명", "회사")):
        set_setting(k, v)
    try:
        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
        mailer.send("u@example.test", "제목", "본문", html="<p>본문</p>", from_name="BMS시스템", plain=True)
        m = sent["msg"]
        assert "BMS" in m["From"] and m["Cc"] is None
        assert m.get_body(("plain",)).get_content().strip() == "본문"
        assert "<p>본문</p>" in m.get_body(("html",)).get_content()
    finally:
        for k, v in before.items():
            set_setting(k, v or "")


def test_email_code_login(mail):
    _user("mf_mail", "mf.mail@example.test")
    with TestClient(app) as c:
        r = _login(c, "mf_mail")
        assert r.status_code == 303 and r.headers["location"].startswith("/login/2fa")
        assert len(mail) == 1 and mail[0][0] == "mf.mail@example.test" and "인증번호" in mail[0][1]
        code = _code(mail)
        assert c.get("/hr/dashboard", follow_redirects=False).headers["location"] == "/login/2fa"   # 아직 못 들어감
        page = c.get("/login/2fa?next=/hr/dashboard").text
        assert "mf***@example.test" in page and "메일로 받은 인증번호" in page
        rows = _raw("SELECT mail_code FROM sessions WHERE mail_code IS NOT NULL")
        assert rows and all(code not in r[0] for r in rows)                     # 번호는 해시만 저장
        tok = _csrf(c, "/login/2fa")
        bad = "000000" if code != "000000" else "111111"
        r = c.post("/login/2fa", data={"code": bad, "csrf_token": tok, "next": "/hr/dashboard"}, headers=O)
        assert r.status_code == 401 and "남은 시도 4번" in r.text
        r = c.post("/login/2fa", data={"code": code, "csrf_token": tok, "next": "/hr/dashboard"}, headers=O, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/hr/dashboard"
        assert c.get("/hr/dashboard").status_code == 200
        assert "bms_dev" not in r.cookies                                      # 기억 안 함
        # 같은 번호 다시 못 씀
        assert _raw("SELECT COUNT(*) FROM sessions WHERE mail_code IS NOT NULL")[0][0] == 0


def test_resend_limits_and_five_wrong(mail):
    _user("mf_lim", "mf.lim@example.test")
    with TestClient(app) as c:
        _login(c, "mf_lim")
        tok = _csrf(c, "/login/2fa")
        r = c.post("/login/2fa/send", data={"csrf_token": tok, "next": "/"}, headers=O, follow_redirects=False)
        assert "30%EC%B4%88" in r.headers["location"] or "30초" in r.headers["location"]   # 30초 안에는 다시 안 보냄
        assert len(mail) == 1
        for i in range(5):
            r = c.post("/login/2fa", data={"code": "999999" if _code(mail) != "999999" else "888888", "csrf_token": tok}, headers=O,
                       follow_redirects=False)
        # 5번 틀리면 계정 잠금(기존 OTP 규칙과 같음) → 로그인 화면
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert auth.query_one("SELECT locked_until FROM users WHERE username='mf_lim'")["locked_until"]


def test_remember_pc_skips_then_password_change_clears(mail):
    uid = _user("mf_rem", "mf.rem@example.test")
    with TestClient(app) as c:
        _login(c, "mf_rem")
        tok = _csrf(c, "/login/2fa")
        r = c.post("/login/2fa", data={"code": _code(mail), "remember": "1", "csrf_token": tok, "next": "/hr/dashboard"},
                   headers=O, follow_redirects=False)
        assert r.status_code == 303 and "bms_dev" in r.headers.get("set-cookie", "")
        raw = r.cookies.get("bms_dev") or re.search(r"bms_dev=([^;]+)", r.headers["set-cookie"]).group(1)
        assert _raw("SELECT COUNT(*) FROM mfa_devices WHERE user_id=?", (uid,))[0][0] == 1
        assert all(raw not in x[0] for x in _raw("SELECT token_hash FROM mfa_devices"))
        c.post("/logout", data={"csrf_token": _csrf(c, "/account")}, headers=O)
        n = len(mail)
        c.cookies.set("bms_dev", raw)
        r = _login(c, "mf_rem")
        assert r.headers["location"] == "/hr/dashboard" and len(mail) == n        # 메일 없이 바로 들어감
        assert "기억한 PC" in c.get("/account").text
        # 비밀번호 바꾸면 기억한 PC 해제
        tok = _csrf(c, "/account/password")
        c.post("/account/password", data={"current": PW, "new": "Mf-Changed-Pass-2!", "new2": "Mf-Changed-Pass-2!", "csrf_token": tok}, headers=O)
        assert _raw("SELECT COUNT(*) FROM mfa_devices WHERE user_id=?", (uid,))[0][0] == 0
        auth.set_password(uid, PW, must_change=False)


def test_fallbacks_without_mail(mail, monkeypatch):
    # 메일 주소 없음 + 앱 없음 → (테스트 설정은 2단계 필수가 아니므로) 바로 로그인
    _user("mf_none", None)
    with TestClient(app) as c:
        assert _login(c, "mf_none").headers["location"] == "/hr/dashboard"
    monkeypatch.setattr(auth.settings, "require_2fa", True)
    with TestClient(app) as c:
        assert _login(c, "mf_none").headers["location"] == "/account/2fa"     # 2단계 필수면 앱 등록으로
    # 앱이 있는 사용자: 방식이 '인증 앱'이면 앱 코드로
    uid = _user("mf_app", "mf.app@example.test")
    secret = auth.new_totp_secret()
    auth.enable_totp(uid, secret)
    set_setting("mfa_method", "app")
    with TestClient(app) as c:
        n = len(mail)
        r = _login(c, "mf_app")
        assert r.headers["location"].startswith("/login/2fa") and len(mail) == n
        assert "인증 앱 코드" in c.get("/login/2fa").text
        tok = _csrf(c, "/login/2fa")
        r = c.post("/login/2fa", data={"code": auth.totp_now(secret), "method": "app", "csrf_token": tok}, headers=O, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
    # 메일 방식이어도 앱으로 바꿔 확인 가능
    set_setting("mfa_method", "email")
    execute("UPDATE users SET totp_last_step=NULL WHERE id=?", (uid,))
    with TestClient(app) as c:
        _login(c, "mf_app")
        assert "인증 앱으로 하기" in c.get("/login/2fa").text
        assert "인증 앱 코드" in c.get("/login/2fa?use=app").text


def test_admin_settings(mail):
    _user("mf_adm", "mf.adm@example.test")
    execute("UPDATE users SET role='admin' WHERE username='mf_adm'")
    target = _user("mf_tgt", None)
    with TestClient(app) as c:
        _login(c, "mf_adm")
        tok = _csrf(c, "/login/2fa")
        c.post("/login/2fa", data={"code": _code(mail), "csrf_token": tok}, headers=O)
        tok = _csrf(c, "/admin/users")
        page = c.get("/admin/users").text
        assert "2단계 인증 방식" in page and "mf_tgt" in page
        r = c.post(f"/admin/users/{target}/update", data={"display_name": "mf_tgt", "role": "hr", "is_active": "1",
                                                          "email": "not-mail", "csrf_token": tok}, headers=O, follow_redirects=False)
        assert "%ED%98%95%EC%8B%9D" in r.headers["location"] or "형식" in r.headers["location"]
        c.post(f"/admin/users/{target}/update", data={"display_name": "mf_tgt", "role": "hr", "is_active": "1",
                                                      "email": "mf.tgt@example.test", "csrf_token": tok}, headers=O)
        assert auth.get_user(target)["email"] == "mf.tgt@example.test"
        c.post("/admin/mfa-method", data={"method": "app", "csrf_token": tok}, headers=O)
        assert mfa.system_method() == "app"
        c.post("/admin/mfa-method", data={"method": "email", "csrf_token": tok}, headers=O)
        r = c.post("/admin/users", data={"username": "mf_new", "display_name": "새사람", "role": "ga", "email": "mf.new@example.test",
                                         "csrf_token": tok}, headers=O, follow_redirects=False)
        assert r.status_code == 303
        assert auth.query_one("SELECT email FROM users WHERE username='mf_new'")["email"] == "mf.new@example.test"
