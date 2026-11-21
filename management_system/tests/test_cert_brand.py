"""증명서: BMS 저장과 폴더 보관 분리, 도장·로고 그림, 양식 2판으로 바꾸기."""
import re

import pytest
from fastapi.testclient import TestClient

from app import brand, mailer, seed, storage
from app.config import settings
from app.db import execute, get_settings, query, query_one, set_setting
from app.main import app
from app.modules.hr import repo
from app.modules.hr.default_templates import CERT_EMPLOYMENT, CERT_EMPLOYMENT_V1
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _client(user, role):
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (user,)):
        auth.create_user(user, user, role, "Strong-Pass-123!", must_change=False)
    c.post("/login", data={"username": user, "password": "Strong-Pass-123!"}, headers=ORIGIN)
    c.headers.update(ORIGIN)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
    return c


@pytest.fixture(scope="module")
def clients():
    with TestClient(app):
        yield {r: _client("cb_" + r, r) for r in ("admin", "hr", "viewer")}


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "render_pdf", lambda content, title: b"%PDF-1.7 " + content.encode()[:60])
    monkeypatch.setattr(settings, "employee_root", str(tmp_path))
    return tmp_path


def _issue(c, **extra):
    emp = next(e for e in repo.list_employees(status="재직(전체)") if e.get("email"))
    tpl = repo.list_templates("certificate", active_only=True)[0]
    r = c.post("/hr/certificates/new", data={"employee_id": emp["id"], "template_id": tpl["id"], "purpose": "제출용", **extra},
               follow_redirects=False)
    return int(r.headers["location"].split("/")[-1].split("?")[0]), emp


def test_save_stays_in_bms_and_folder_only_by_button(clients, root, monkeypatch):
    c = clients["hr"]
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, attachments=None, **k: sent.append(attachments))
    doc_id, emp = _issue(c, mail_mode="employee")
    assert sent and sent[-1][0][1].startswith(b"%PDF")                       # 메일에는 PDF 가 붙고
    assert not repo.get_document(doc_id)["pdf_path"] and not list(root.rglob("*.pdf"))   # 발급·메일만으로는 폴더에 저장하지 않음
    r = c.post(f"/hr/api/documents/{doc_id}", json={"content": "<p>고친 내용</p>", "status": "발급완료"})
    assert r.json()["message"].startswith("BMS에 저장") and not list(root.rglob("*.pdf"))    # [저장]도 BMS 에만
    c.post(f"/hr/documents/{doc_id}/email", data={"target": "employee"})
    assert "고친 내용".encode() in sent[-1][0][1] and not list(root.rglob("*.pdf"))
    page = c.get(f"/hr/documents/{doc_id}").text
    assert "BMS에 저장" in page and "폴더에 저장한 PDF 열기" not in page
    # 버튼을 눌렀을 때만 폴더에 저장
    c.post(f"/hr/documents/{doc_id}/pdf")
    d = repo.get_document(doc_id)
    files = list(root.rglob("*.pdf"))
    assert len(files) == 1 and d["pdf_path"] == str(files[0]) and files[0].parent.name == (emp.get("folder_name") or emp["name"])
    page = c.get(f"/hr/documents/{doc_id}").text
    assert "폴더에 저장한 PDF 열기" in page and files[0].name in page and "다시 누르세요" not in page
    assert c.get(f"/hr/documents/{doc_id}/pdf").content.startswith(b"%PDF")
    # 발급취소·삭제는 BMS 대장만 바꾸고 폴더 파일은 그대로
    c.post(f"/hr/api/documents/{doc_id}", json={"content": "<p>고친 내용 2</p>", "status": "발급취소"})
    assert files[0].exists() and repo.get_document(doc_id)["status"] == "발급취소"
    execute("UPDATE hr_documents SET pdf_saved_at='2000-01-01 00:00:00' WHERE id=?", (doc_id,))     # 같은 초에 일어난 일을 구분
    assert "다시 누르세요" in c.get(f"/hr/documents/{doc_id}").text           # 폴더 저장 뒤 고쳤음을 알림
    r = c.post(f"/hr/documents/{doc_id}/delete", follow_redirects=False)
    assert repo.get_document(doc_id) is None and files[0].exists() and "msg=" in r.headers["location"]


def test_brand_upload_roles_and_validation(clients):
    admin, hr, viewer = clients["admin"], clients["hr"], clients["viewer"]
    try:
        assert hr.post("/settings/brand/seal", files={"file": ("s.png", PNG, "image/png")}, follow_redirects=False).status_code == 403
        assert brand.get("seal") is None and hr.get("/hr/api/doc-asset/seal").status_code == 404
        for name, raw in (("a.svg", b"<svg onload=alert(1)>"), ("a.png", b"GIF89a" + b"0" * 20), ("big.png", PNG + b"0" * brand.MAX_BYTES), ("e.png", b"")):
            admin.post("/settings/brand/seal", files={"file": (name, raw, "image/png")})
            assert brand.get("seal") is None, name                           # 그림이 아니거나 너무 크면 저장 안 함
        assert admin.post("/settings/brand/other", files={"file": ("s.png", PNG, "image/png")}, follow_redirects=False).status_code == 404
        admin.post("/settings/brand/seal", files={"file": ("s.png", PNG, "image/png")})
        admin.post("/settings/brand/logo", files={"file": ("l.jpg", JPG, "image/jpeg")})
        assert brand.get("seal") == (PNG, "image/png") and brand.get("logo") == (JPG, "image/jpeg")
        raw = query_one("SELECT value FROM settings WHERE key='doc_seal'")["value"]
        import sqlite3
        stored = sqlite3.connect(settings.db_path).execute("SELECT value FROM settings WHERE key='doc_seal'").fetchone()[0]
        assert stored.startswith("enc:v1:") and raw.startswith("image/png;")   # DB 에는 암호화
        r = hr.get("/hr/api/doc-asset/seal")
        assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
        assert viewer.get("/hr/api/doc-asset/seal", follow_redirects=False).status_code == 403
        assert hr.get("/hr/api/doc-asset/nope").status_code == 404
        screen, pdf = brand.css(), brand.css(embed=True)
        assert "/hr/api/doc-asset/seal?v=" in screen and ".doc-logo" in screen and "data:" not in screen
        assert "url(data:image/png;base64," in pdf and "url(data:image/jpeg;base64," in pdf
        assert "data:image/png;base64," in storage._paper_css()             # PDF 에 도장이 들어감
        page = admin.get("/settings").text
        assert "증명서 도장" in page and 'src="/hr/api/doc-asset/seal"' in page and "그림 지우기" in page
        doc_id, _ = _issue(hr)
        page = hr.get(f"/hr/documents/{doc_id}").text
        assert "/hr/api/doc-asset/seal?v=" in page and 'class="doc-seal"' in page and "cert-table" in page
        execute("DELETE FROM hr_documents WHERE id=?", (doc_id,))
        admin.post("/settings/brand/seal", data={"remove": "1"})
        assert brand.get("seal") is None and ".doc-seal" not in brand.css()
    finally:
        set_setting("doc_seal", "")
        set_setting("doc_logo", "")


def test_cert_template_upgrade():
    keep = {t["id"]: dict(t) for t in query("SELECT * FROM doc_templates WHERE doc_group='certificate'")}
    try:
        # 고치지 않은 예전 기본 양식은 그 자리에서 새 양식으로
        execute("UPDATE doc_templates SET content=? WHERE doc_group='certificate' AND category='재직증명'", (CERT_EMPLOYMENT_V1.strip(),))
        # 직접 고친 양식은 꺼 두고(지우지 않음) 새 양식을 추가
        execute("UPDATE doc_templates SET content=? WHERE doc_group='certificate' AND category='경력증명'", ("<p>우리 회사가 고친 경력증명서 {{성명}}</p>",))
        set_setting("cert_tpl_ver", "")
        seed.upgrade_cert_templates()
        emp_t = query("SELECT * FROM doc_templates WHERE doc_group='certificate' AND category='재직증명'")
        assert len(emp_t) == 1 and emp_t[0]["content"] == CERT_EMPLOYMENT.strip() and emp_t[0]["is_active"]
        car = query("SELECT * FROM doc_templates WHERE doc_group='certificate' AND category='경력증명' ORDER BY id")
        assert len(car) == 2 and car[0]["name"].endswith("(이전 양식)") and not car[0]["is_active"] and "우리 회사가 고친" in car[0]["content"]
        assert car[1]["is_active"] and "doc-seal" in car[1]["content"] and "사업자등록번호" not in car[1]["content"]
        assert get_settings()["cert_tpl_ver"] == "2"
        n = query_one("SELECT COUNT(*) c FROM doc_templates")["c"]
        set_setting("cert_tpl_ver", "")
        seed.upgrade_cert_templates()                                     # 다시 돌려도 더 늘지 않음
        assert query_one("SELECT COUNT(*) c FROM doc_templates")["c"] == n
    finally:
        execute("DELETE FROM doc_templates WHERE doc_group='certificate' AND id NOT IN (%s)" % ",".join(str(i) for i in keep))
        for t in keep.values():
            execute("UPDATE doc_templates SET name=?, content=?, is_active=? WHERE id=?", (t["name"], t["content"], t["is_active"], t["id"]))


def test_new_cert_template_survives_cleaning():
    from app.common import sanitize_html
    out = sanitize_html(CERT_EMPLOYMENT)
    for cls in ("doc-head", "doc-logo", "doc-no", "cert-title", "cert-table", "cert-sign", "doc-seal", "cert-foot"):
        assert f'class="{cls}"' in out or f"{cls}\"" in out, cls
    assert "{{사업자번호}}" not in CERT_EMPLOYMENT and "(직인)" in out
