"""증명서: BMS 저장과 폴더 보관 분리, 도장·로고 그림, 양식 2판으로 바꾸기."""
import io
import re

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import brand, mailer, seed, storage
from app.config import settings
from app.db import execute, get_settings, query, query_one, set_setting
from app.main import app
from app.modules.hr import repo
from app.modules.hr.default_templates import CERT_EMPLOYMENT, CERT_EMPLOYMENT_V1
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
def _img(fmt, size=(60, 40), color=(200, 30, 30), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


PNG, JPG = _img("PNG"), _img("JPEG")


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
    c.post(f"/hr/api/documents/{doc_id}", json={"content": "<p>고친 내용 2</p>"})
    execute("UPDATE hr_documents SET pdf_saved_at='2000-01-01 00:00:00' WHERE id=?", (doc_id,))     # 같은 초에 일어난 일을 구분
    assert "다시 누르세요" in c.get(f"/hr/documents/{doc_id}").text           # 폴더 저장 뒤 고쳤음을 알림
    # [저장]으로는 상태를 못 바꿈. 발급취소는 사유와 함께, 대장·번호·발송 기록·폴더 파일은 그대로
    c.post(f"/hr/api/documents/{doc_id}", json={"status": "발급취소"})
    assert repo.get_document(doc_id)["status"] == "발급완료"
    c.post(f"/hr/documents/{doc_id}/cancel", data={"reason": "  "})
    assert repo.get_document(doc_id)["status"] == "발급완료"                  # 사유 없이는 취소 안 됨
    no = repo.get_document(doc_id)["doc_no"]
    c.post(f"/hr/documents/{doc_id}/cancel", data={"reason": "용도 변경으로 재발급"})
    d = repo.get_document(doc_id)
    assert d["status"] == "발급취소" and d["cancel_reason"] == "용도 변경으로 재발급" and d["canceled_at"] and d["canceled_by"]
    assert files[0].exists() and repo.list_doc_mails(doc_id)
    page = c.get("/hr/certificates").text
    assert no in page and "용도 변경으로 재발급" in page
    n = len(sent)
    c.post(f"/hr/documents/{doc_id}/email", data={"target": "employee"})
    c.post(f"/hr/api/documents/{doc_id}", json={"content": "<p>취소 뒤 고침</p>"})
    assert len(sent) == n and "취소 뒤 고침" not in repo.get_document(doc_id)["content"]     # 취소본은 보내지도 고치지도 않음
    # 보냈거나 폴더에 저장한 증명서는 삭제 불가
    r = c.post(f"/hr/documents/{doc_id}/delete", follow_redirects=False)
    assert repo.get_document(doc_id) and f"/hr/documents/{doc_id}" in r.headers["location"]
    assert "삭제할 수 없습니다" in c.get(f"/hr/documents/{doc_id}").text
    # 다음 발급은 다음 번호 (취소한 번호를 다시 쓰지 않음)
    doc2, _ = _issue(c)
    no2 = repo.get_document(doc2)["doc_no"]
    assert int(no2[-5:-1]) == int(no[-5:-1]) + 1
    # 아직 안 나간 마지막 번호 증명서를 지우면 그 번호를 다시 씀
    c.post(f"/hr/documents/{doc2}/delete")
    assert repo.get_document(doc2) is None
    doc3, _ = _issue(c)
    assert repo.get_document(doc3)["doc_no"] == no2
    # 중간 번호를 지우면 번호를 비워 둠
    doc4, _ = _issue(c)
    c.post(f"/hr/documents/{doc3}/delete")
    doc5, _ = _issue(c)
    assert int(repo.get_document(doc5)["doc_no"][-5:-1]) == int(repo.get_document(doc4)["doc_no"][-5:-1]) + 1
    c.post(f"/hr/documents/{doc_id}/cancel", data={"restore": "1"})
    assert repo.get_document(doc_id)["status"] == "발급완료" and not repo.get_document(doc_id)["cancel_reason"]


def test_brand_upload_roles_and_validation(clients):
    admin, hr, viewer = clients["admin"], clients["hr"], clients["viewer"]
    try:
        assert hr.post("/settings/brand/seal", files={"file": ("s.png", PNG, "image/png")}, follow_redirects=False).status_code == 403
        assert brand.get("seal") is None and hr.get("/hr/api/doc-asset/seal").status_code == 404
        for name, raw in (("a.svg", b"<svg onload=alert(1)>"), ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40), ("a.tif", _img("TIFF")),
                          ("big.png", PNG + b"0" * brand.MAX_BYTES), ("e.png", b"")):
            admin.post("/settings/brand/seal", files={"file": (name, raw, "image/png")})
            assert brand.get("seal") is None, name                           # 그림이 아니거나 너무 크면 저장 안 함
        assert admin.post("/settings/brand/other", files={"file": ("s.png", PNG, "image/png")}, follow_redirects=False).status_code == 404
        admin.post("/settings/brand/seal", files={"file": ("s.png", PNG, "image/png")})
        admin.post("/settings/brand/logo", files={"file": ("l.jpg", JPG, "image/jpeg")})
        seal, logo = brand.get("seal"), brand.get("logo")
        assert seal[1] == logo[1] == "image/png" and seal[0] != PNG                  # 올린 파일 그대로가 아니라 다시 그린 PNG
        assert Image.open(io.BytesIO(logo[0])).size == (60, 40)
        PNG_OUT = seal[0]
        r = admin.get("/settings?bmsg=x&bok=1")
        raw = query_one("SELECT value FROM settings WHERE key='doc_seal'")["value"]
        import sqlite3
        stored = sqlite3.connect(settings.db_path).execute("SELECT value FROM settings WHERE key='doc_seal'").fetchone()[0]
        assert stored.startswith("enc:v1:") and raw.startswith("image/png;")   # DB 에는 암호화
        r = hr.get("/hr/api/doc-asset/seal")
        assert r.status_code == 200 and r.content == PNG_OUT and r.headers["content-type"] == "image/png"
        assert viewer.get("/hr/api/doc-asset/seal", follow_redirects=False).status_code == 403
        assert hr.get("/hr/api/doc-asset/nope").status_code == 404
        screen, pdf = brand.css(), brand.css(embed=True)
        assert "/hr/api/doc-asset/seal?v=" in screen and ".doc-logo" in screen and "data:" not in screen
        assert pdf.count("url(data:image/png;base64,") == 2
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


def test_brand_resizes_and_clears_white_paper():
    try:
        big = Image.new("RGB", (2400, 1800), (255, 255, 255))
        big.paste((190, 20, 20), (800, 500, 1600, 1300))                     # 흰 종이에 찍은 붉은 도장
        big.paste((190, 20, 20), (200, 300, 300, 400))                       # 떨어져 찍힌 작은 자국
        buf = io.BytesIO()
        big.save(buf, "JPEG", quality=90)
        assert brand.save("seal", buf.getvalue()) == ""
        out = Image.open(io.BytesIO(brand.get("seal")[0]))
        assert out.size == (500, 357) and out.mode == "RGBA"                 # 둘레 흰 곳을 잘라내고 줄여서 (1400x1000 → 500x357)
        assert out.getpixel((120, 20))[3] == 0 and out.getpixel((350, 200))[3] == 255     # 흰 바탕은 투명, 도장은 그대로
        clear = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        clear.paste((255, 255, 255, 255), (40, 40, 60, 60))                  # 이미 투명한 그림은 흰 부분도 그대로 둠
        buf = io.BytesIO()
        clear.save(buf, "PNG")
        assert brand.save("seal", buf.getvalue()) == ""
        assert Image.open(io.BytesIO(brand.get("seal")[0])).getpixel((10, 10)) == (255, 255, 255, 255)
    finally:
        set_setting("doc_seal", "")


def test_pdf_css_keeps_page_margin():
    css = storage._paper_css()
    assert css.rindex("@page{size:A4;margin:20mm}") > css.rindex("margin: 0; }")      # 인쇄용 여백 0 보다 뒤에 와야 이김


def test_reward_and_special_edit_filter_and_card(clients):
    hr = clients["hr"]
    emps = [e for e in repo.list_employees(status="재직(전체)") if e.get("department")]
    a = emps[0]
    b = next(e for e in emps if e["department"] != a["department"] and e["name"] != a["name"])
    for e, reason in ((a, "상벌수정시험 가"), (b, "상벌수정시험 나")):
        hr.post("/hr/rewards/save", data={"employee_id": e["id"], "kind": "포상", "category": "표창", "action_date": "2026-03-02", "reason": reason, "issuer": "대표이사"})
    mine = lambda e: [r for r in repo.list_rewards(employee_id=e["id"]) if (r["reason"] or "").startswith("상벌수정시험")]   # noqa: E731
    ra = mine(a)[0]
    # 수정: 같은 id 로 저장하면 새로 생기지 않고 바뀜
    hr.post("/hr/rewards/save", data={"id": ra["id"], "employee_id": a["id"], "kind": "포상", "category": "표창", "action_date": "2026-03-05",
                                      "reason": "상벌수정시험 가(고침)", "amount": "300000", "issuer": "대표이사"})
    assert len(mine(a)) == 1
    got = mine(a)[0]
    assert got["reason"] == "상벌수정시험 가(고침)" and got["action_date"] == "2026-03-05" and int(got["amount"]) == 300000
    page = hr.get("/hr/rewards").text
    assert "data-reward-edit=" in page and "상벌수정시험 가(고침)" in page and "상벌수정시험 나" in page
    page = hr.get("/hr/rewards", params={"q": a["name"]}).text
    assert "상벌수정시험 가(고침)" in page and "상벌수정시험 나" not in page                # 이름으로 조회
    page = hr.get("/hr/rewards", params={"department": b["department"]}).text
    assert "상벌수정시험 나" in page and "상벌수정시험 가(고침)" not in page                # 부서로 조회
    card = hr.get(f"/hr/employees/{a['id']}?tab=reward").text
    assert "data-reward-edit=" in card and "/hr/rewards?q=" in card and "관련 메뉴" in card
    # 근로특이사항
    hr.post("/hr/specials/save", data={"employee_id": a["id"], "category": "육아휴직", "start_date": "2026-05-01", "end_date": "2026-10-31", "note": "특이수정시험"})
    sps = lambda: [x for x in repo.list_specials(employee_id=a["id"]) if x["note"] == "특이수정시험"]   # noqa: E731
    sp = sps()[0]
    hr.post("/hr/specials/save", data={"id": sp["id"], "employee_id": a["id"], "category": "육아휴직", "start_date": "2026-05-01", "end_date": "2026-11-30", "note": "특이수정시험"})
    assert len(sps()) == 1 and sps()[0]["end_date"] == "2026-11-30"
    assert "data-special-edit=" in hr.get("/hr/specials", params={"q": a["name"]}).text
    assert "data-special-edit=" not in hr.get("/hr/specials", params={"q": "없는이름zz"}).text
    assert "data-special-edit=" in hr.get(f"/hr/employees/{a['id']}?tab=special").text
    # 교육이수 탭 (보기 전용)
    t = hr.get(f"/hr/employees/{a['id']}?tab=training")
    assert t.status_code == 200 and "교육 이수 현황" in t.text and "이수 기록" in t.text and "/mgmt/training" in t.text
    v = clients["viewer"].get(f"/hr/employees/{a['id']}?tab=training").text
    assert "교육 이수 현황" not in v and "관련 메뉴" not in v
    for r in mine(a) + mine(b):
        repo.delete_reward(r["id"])
    repo.delete_special(sp["id"])
