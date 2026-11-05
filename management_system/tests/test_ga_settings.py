"""자산 사진·이력 수정·부가세, 구분값 설정, 메일 설정, 카택스 연동, 입사서류·체크리스트 테스트."""
import io
import json
import re
import smtplib
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import mailer
from app.common import vat_split
from app.config import settings
from app.db import get_behaviors, get_codes, get_settings, set_setting
from app.main import app
from app.modules.ga import cartax, photos, repo, services
from app.modules.hr import intake
from app.modules.hr import repo as hr_repo
from app.modules.hr import services as hr_services
from app.security import auth

ORIGIN = {"origin": "http://testserver"}


def _client(user, role):
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (user,)):
        auth.create_user(user, user, role, "Strong-Pass-123!", must_change=False)
    c.post("/login", data={"username": user, "password": "Strong-Pass-123!"}, headers=ORIGIN)
    c.headers.update(ORIGIN)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
    return c


@pytest.fixture(scope="module")
def admin():
    with TestClient(app):
        yield _client("cfg_admin", "admin")


@pytest.fixture(scope="module")
def ga(admin):
    return _client("cfg_ga", "ga")


def _png(w=3000, h=2000):
    buf = io.BytesIO()
    Image.effect_noise((w, h), 60).convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _asset(ga, **kw):
    r = ga.post("/ga/assets/save", data={"category": "IT기기", "name": "노트북", "acquired_date": "2026-01-15",
                                         "cost": "1100000", "cost_vat": "부가세포함", **kw}, follow_redirects=False)
    return int(re.search(r"/ga/assets/(\d+)", r.headers["location"]).group(1))


def test_vat_split():
    assert vat_split(1100000, "부가세포함") == {"supply": 1000000, "vat": 100000, "total": 1100000}
    assert vat_split(1000000, "부가세별도") == {"supply": 1000000, "vat": 100000, "total": 1100000}
    assert vat_split(1000000, "면세") == {"supply": 1000000, "vat": 0, "total": 1000000}
    assert vat_split(1000000, None) == {"supply": 1000000, "vat": 0, "total": 1000000}


def test_asset_vat_uses_supply_for_depreciation(ga):
    aid = _asset(ga)
    a = services.enrich_assets(repo.list_assets(q=repo.get("ga_assets", aid)["asset_no"]))[0]
    assert a["supply"] == 1000000 and a["vat"] == 100000
    page = ga.get(f"/ga/assets/{aid}").text
    assert "공급가 1,000,000 + 부가세 100,000" in page
    assert "부가세구분" in ga.get("/ga/assets/export.csv").text


def test_asset_photo_shrinks_and_drops_on_delete(ga):
    raw = _png()
    assert len(raw) > 1_000_000
    aid = _asset(ga)
    r = ga.post(f"/ga/assets/{aid}/photo", files={"photo_file": ("a.jpg", raw, "image/jpeg")}, follow_redirects=False)
    assert "사진" in r.headers["location"] or r.status_code == 303
    assert repo.get("ga_assets", aid)["photo"]
    import os
    assert os.path.getsize(photos.path(aid)) < 300_000 and os.path.getsize(photos.path(aid, True)) < 20_000
    with Image.open(photos.path(aid)) as img:
        assert max(img.size) == photos.MAX_SIDE
    assert ga.get(f"/ga/assets/{aid}/photo").headers["content-type"] == "image/webp"
    assert "/photo" in ga.get(f"/ga/assets/{aid}").text
    bad = ga.post(f"/ga/assets/{aid}/photo", files={"photo_file": ("a.jpg", b"not image", "image/jpeg")}, follow_redirects=False)
    assert bad.status_code == 303
    ga.post(f"/ga/assets/{aid}/delete", data={"next": "/ga/assets"})
    assert not os.path.exists(photos.path(aid))


def test_asset_register_with_photo(ga):
    r = ga.post("/ga/assets/save", data={"category": "기타", "name": "복합기", "cost": "1000", "cost_vat": "면세"},
                files={"photo_file": ("a.jpg", _png(800, 600), "image/jpeg")}, follow_redirects=False)
    aid = int(re.search(r"/ga/assets/(\d+)", r.headers["location"]).group(1))
    assert repo.get("ga_assets", aid)["photo"]
    photos.delete(aid)


def test_history_edit_delete_rebuilds_state(ga):
    emp = repo.employees_brief()[0]
    aid = _asset(ga)
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "지급", "event_date": "2026-02-01", "holder_id": emp["id"]})
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "수리", "event_date": "2026-03-01", "cost": "50000", "cost_vat": "부가세별도"})
    a = repo.get("ga_assets", aid)
    assert a["status"] == "수리중" and a["holder_id"] == emp["id"]
    hist = repo.asset_history(aid)
    repair = next(h for h in hist if h["event_type"] == "수리")
    assert repair["cost_vat"] == "부가세별도"

    ga.post(f"/ga/assets/{aid}/history/{repair['id']}", data={"event_type": "점검", "event_date": "2026-03-02", "cost": "60000", "note": "수정"})
    h = repo.get("ga_asset_history", repair["id"])
    assert h["event_type"] == "점검" and h["cost"] == 60000 and h["note"] == "수정"
    assert repo.get("ga_assets", aid)["status"] == "사용중"      # 수리 이력이 점검으로 바뀌어 상태 복구

    give = next(h for h in repo.asset_history(aid) if h["event_type"] == "지급")
    ga.post(f"/ga/assets/{aid}/history/{give['id']}/delete")
    a = repo.get("ga_assets", aid)
    assert a["holder_id"] is None and a["status"] == "보관"

    other = _asset(ga)
    wrong = ga.post(f"/ga/assets/{other}/history/{repair['id']}/delete")
    assert wrong.status_code == 404                               # 다른 자산의 이력은 처리 불가


def test_repair_done_event(ga):
    aid = _asset(ga)
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "수리", "event_date": "2026-03-01"})
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "수리완료", "event_date": "2026-03-05"})
    assert repo.get("ga_assets", aid)["status"] == "보관"


def _code(group, name):
    return next(c for c in hr_repo.list_codes_all() if c["group_code"] == group and c["name"] == name)


def test_code_settings_rename_propagates_and_keeps_behavior(admin, ga):
    aid = _asset(ga)
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "수리", "event_date": "2026-03-01"})
    code = _code("ASSET_EVENT", "수리")
    admin.post(f"/settings/codes/{code['id']}/rename", data={"name": "수선"})
    assert "수선" in get_codes("ASSET_EVENT") and get_behaviors("ASSET_EVENT")["수선"] == "수리"
    assert any(h["event_type"] == "수선" for h in repo.asset_history(aid))
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "수선", "event_date": "2026-04-01"})
    assert repo.get("ga_assets", aid)["status"] == "수리중"
    assert "수선" in ga.get(f"/ga/assets/{aid}").text
    admin.post(f"/settings/codes/{code['id']}/rename", data={"name": "수리"})
    # 같은 이름으로 변경 불가
    dup = _code("ASSET_EVENT", "지급")
    r = admin.post(f"/settings/codes/{dup['id']}/rename", data={"name": "반납"}, follow_redirects=False)
    assert "%EC%9D%B4%EB%AF%B8" in r.headers["location"]


def test_code_settings_add_move_and_used_by_screens(admin):
    admin.post("/settings/codes", data={"group_code": "EMPLOYMENT_TYPE", "name": "파견직"})
    assert "파견직" in admin.get("/hr/employees/new").text
    admin.post("/settings/codes", data={"group_code": "REWARD_징계", "name": "구두주의"})
    assert json.dumps("구두주의")[1:-1] in admin.get("/hr/rewards").text      # 모달 스크립트의 JSON
    code = _code("MAINT_CATEGORY", "소모품")
    admin.post(f"/settings/codes/{code['id']}/move", data={"dir": "-1"})
    assert get_codes("MAINT_CATEGORY")[0] == "소모품"
    behavior = _code("ASSET_EVENT", "이동")
    admin.post(f"/settings/codes/{behavior['id']}/behavior", data={"behavior": "반납"})
    assert get_behaviors("ASSET_EVENT")["이동"] == "반납"
    admin.post(f"/settings/codes/{behavior['id']}/behavior", data={"behavior": ""})


def test_settings_page_and_permissions(admin, ga):
    assert "구분값 설정" in admin.get("/settings").text
    assert ga.get("/settings").status_code == 403
    assert ga.post("/settings/mail", data={"mail_host": "x"}).status_code == 403


def test_mail_settings_encrypted_and_password_kept(admin):
    admin.post("/settings/mail", data={"mail_host": "smtp.mailplug.co.kr", "mail_port": "465", "mail_user": "a@b.kr",
                                       "mail_password": "abcd efgh", "mail_from_name": "회사", "mail_cc": "c@b.kr",
                                       "mail_signature": "서명", "public_url": "https://bms.test"})
    conn = sqlite3.connect(settings.db_path)
    raw = conn.execute("SELECT value FROM settings WHERE key='mail_password'").fetchone()[0]
    conn.close()
    assert raw.startswith("enc:")
    admin.post("/settings/mail", data={"mail_host": "smtp.mailplug.co.kr", "mail_port": "465", "mail_user": "a@b.kr", "mail_password": ""})
    c = mailer.config()
    assert c["password"] == "abcdefgh" and c["ssl"] is True and c["from"] == "a@b.kr" and mailer.configured()
    page = admin.get("/settings").text
    assert "abcdefgh" not in page and "저장됨" in page


def test_mail_send_uses_settings(admin, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            sent.update(host=host, port=port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            sent.update(user=user, pw=pw)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    mailer.send("to@b.kr", "제목", "본문")
    assert sent["host"] == "smtp.mailplug.co.kr" and sent["port"] == 465 and sent["pw"] == "abcdefgh"
    assert sent["msg"]["Cc"] == "c@b.kr" and "서명" in sent["msg"].get_content()
    r = admin.post("/settings/mail/test", data={"to": "to@b.kr"}, follow_redirects=False)
    assert r.status_code == 303

    class Reject(FakeSMTP):
        def login(self, user, pw):
            raise smtplib.SMTPAuthenticationError(535, b"no")

    monkeypatch.setattr(smtplib, "SMTP_SSL", Reject)
    with pytest.raises(mailer.MailError, match="앱 비밀번호"):
        mailer.send("to@b.kr", "제목", "본문")
    for k in ("mail_host", "mail_user", "mail_password", "mail_cc", "mail_signature", "public_url"):
        set_setting(k, "")


def test_qr_uses_public_url(admin, ga):
    aid = _asset(ga)
    no = repo.get("ga_assets", aid)["asset_no"]
    assert f'data-text="{no}"' in ga.get(f"/ga/assets/{aid}").text
    set_setting("public_url", "https://bms.test")
    assert f'data-text="https://bms.test/ga/a/{no}"' in ga.get(f"/ga/assets/{aid}").text
    set_setting("public_url", "")
    r = ga.get(f"/ga/a/{no}", follow_redirects=False)
    assert r.headers["location"] == f"/ga/assets/{aid}"
    assert TestClient(app).get(f"/ga/a/{no}", follow_redirects=False).headers["location"].startswith("/login")


CARS = [{"carNumber": "12가 3456", "modelName": "쏘나타", "fuel": "휘발유", "fuelEconomy": 0, "totalDistance": 100}]


def _driving(id_, name, **kw):
    return {"id": id_, "updateTime": "t1", "carNumber": "12가3456", "purpose": "일반업무", "userName": name, "department": "영업",
            "duty": "", "distanceTraveled": 30, "fromDate": "2026-03-02", "fromTime": "2026-03-02T09:00:00Z", "fromOdometer": 1000,
            "toOdometer": 1030, "toAddress": "서울", "toLocation": "고객사", "fuelCost": 1000, "tollgateCost": 200, "etcCost": 300,
            "memo": "메모", **kw}


def test_cartax_helpers():
    assert cartax.purpose_of("출·퇴근") == "출퇴근" and cartax.purpose_of("비업무") == "비업무" and cartax.purpose_of("일반 업무") == "일반업무"
    windows = cartax._windows("2026-01-01", "2026-12-31")
    assert len(windows) == 4 and windows[0] == ("2026-01-01", "2026-04-10") and windows[-1][1] == "2026-12-31"


def test_cartax_sync_imports_and_dedupes(admin, monkeypatch):
    emp = repo.employees_brief()[0]
    calls = []
    rows = [_driving("a1", emp["name"]), _driving("a2", "없는사람", purpose="출퇴근", toOdometer=1000, fromOdometer=1030)]

    def fake(path, key, body=None):
        calls.append((path, key, body))
        return CARS if path == "/cars" else rows

    monkeypatch.setattr(cartax, "_call", fake)
    with pytest.raises(cartax.CartaxError, match="입력되지"):
        cartax.sync("2026-03-01", "2026-03-31")
    admin.post("/settings/cartax", data={"cartax_api_key": "secret-key"})
    assert "secret-key" not in admin.get("/settings").text
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT value FROM settings WHERE key='cartax_api_key'").fetchone()[0].startswith("enc:")
    conn.close()

    msg = cartax.sync("2026-03-01", "2026-03-31")
    assert "신규 2건" in msg and "차량 1대" in msg and calls[0][1] == "secret-key"
    vehicle = next(v for v in repo.list_vehicles() if v["plate_no"] == "12가 3456")
    logs = {l["external_id"]: l for l in repo.vehicle_logs(vehicle["id"])}
    first, second = logs["cartax:a1"], logs["cartax:a2"]
    assert first["driver_id"] == emp["id"] and first["destination"] == "고객사" and first["toll_cost"] == 500
    assert second["purpose"] == "출퇴근" and second["driver_id"] is None and "없는사람" in second["note"]
    assert second["km_end"] == 1060                               # 계기판 역전 시 주행거리로 보정

    assert "신규 0건, 수정 0건" in cartax.sync("2026-03-01", "2026-03-31")
    rows[0]["updateTime"] = "t2"
    rows[0]["toLocation"] = "변경"
    assert "수정 1건" in cartax.sync("2026-03-01", "2026-03-31")
    assert repo.vehicle_logs(vehicle["id"])[0]["id"]
    assert get_settings()["cartax_last_sync"]
    r = admin.post("/ga/vehicles/cartax-sync", data={"start": "2026-03-01", "end": "2026-03-31"}, follow_redirects=False)
    assert r.status_code == 303


def test_cartax_connection_errors(admin, monkeypatch):
    def denied(path, key, body=None):
        raise cartax.CartaxError("카택스 등급 제한입니다")

    monkeypatch.setattr(cartax, "_call", denied)
    r = admin.post("/settings/cartax/test", follow_redirects=False)
    assert r.status_code == 303 and "%EC%97%B0%EA%B2%B0" in r.headers["location"]
    set_setting("cartax_api_key", "")


def test_employee_files_checklist_and_technicians(admin, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "employee_root", str(tmp_path))
    emp = repo.employees_brief()[0]
    folder = tmp_path / emp["name"]
    folder.mkdir()
    (folder / "02. 등본.pdf").write_bytes(b"%PDF-1.7")
    (folder / "BMS_근로계약서_x.pdf").write_bytes(b"%PDF-1.7")
    hr_repo.match_employee_folder(emp["name"])

    page = admin.get(f"/hr/employees/{emp['id']}?tab=files").text
    assert "02. 등본.pdf" in page and "미제출" in page and "시스템 저장" in page
    status = {d["label"]: d["file"] for d in intake.document_status(get_codes("INTAKE_DOC"), intake.list_files(hr_repo.get_employee(emp["id"])))}
    assert status["등본"] == "02. 등본.pdf" and status["통장 / 계좌"] is None
    assert admin.get(f"/hr/employees/{emp['id']}/file", params={"name": "02. 등본.pdf"}).headers["content-type"] == "application/pdf"
    assert admin.get(f"/hr/employees/{emp['id']}/file", params={"name": "..\\..\\secret.txt"}).status_code == 404

    admin.post(f"/hr/employees/{emp['id']}/checks", data={"kind": "ONBOARD", "item": "4대보험 취득신고", "done": "1"})
    assert ("ONBOARD", "4대보험 취득신고") in hr_repo.list_checks(emp["id"])
    assert "checked" in admin.get(f"/hr/employees/{emp['id']}?tab=check").text
    admin.post(f"/hr/employees/{emp['id']}/checks", data={"kind": "ONBOARD", "item": "4대보험 취득신고"})
    assert not hr_repo.list_checks(emp["id"])

    e = hr_repo.get_employee(emp["id"])
    hr_repo.save_qualifications(emp["id"], [{"kind": "전기공사기술자", "grade": "초급", "license_no": "E-1", "expires_on": "2020-01-01"}])
    assert "E-1" in admin.get("/hr/technicians").text and "만료" in admin.get("/hr/technicians").text
    assert "E-1" in admin.get("/hr/technicians/export.csv").text
    alerts = hr_services.build_alerts(hr_repo.list_employees(status=""))
    assert any(a["category"] == "기술자격" and a["name"] == e["name"] for a in alerts)
    hr_repo.save_qualifications(emp["id"], [{"kind": "전기공사기술자"}])
    v = _client("cfg_view", "viewer")
    assert v.get(f"/hr/employees/{emp['id']}/file", params={"name": "02. 등본.pdf"}).status_code == 403
    assert v.get("/hr/technicians").status_code == 403


def test_contract_pay_breakdown_placeholders():
    emp = {"name": "가", "hire_date": "2026-01-01"}
    ctx = hr_services.build_context(emp, {"annual_salary": "60000000", "pay_base": "4500000", "pay_overtime": "500000"})
    assert ctx["기본급"] == "4,500,000" and ctx["연구보조비"] == "0"
