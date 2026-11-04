"""총무관리 모듈 테스트: 화면·자산·차량·문서·관인·계약·경조사·권한."""
import re
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import auth

O = {"origin": "http://testserver"}


def _client(username, role):
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
        auth.create_user(username, username, role, "Ga-Strong-Pass-1!", must_change=False)
    c.post("/login", data={"username": username, "password": "Ga-Strong-Pass-1!"}, headers=O)
    c.headers.update(O)
    tok = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
    c.headers["x-csrf-token"] = tok
    return c


@pytest.fixture(scope="module")
def ga():
    with TestClient(app):
        yield _client("ga_staff", "ga")


@pytest.fixture(scope="module")
def admin():
    with TestClient(app):
        yield _client("ga_admin", "admin")


PAGES = ["/ga/dashboard", "/ga/assets", "/ga/assets/1", "/ga/assets/labels", "/ga/vehicles", "/ga/vehicles/1",
         "/ga/docs", "/ga/docs?tab=seal", "/ga/contracts", "/ga/contracts?status=", "/ga/events",
         "/ga/assets/export.csv", "/ga/contracts/export.csv", "/ga/vehicles/1/logbook.csv?year=2026"]


@pytest.mark.parametrize("url", PAGES)
def test_pages(ga, url):
    r = ga.get(url)
    assert r.status_code == 200, r.text[:300]


def test_asset_lifecycle_and_depreciation(ga):
    r = ga.post("/ga/assets/save", data={"category": "IT기기", "name": "테스트 노트북", "acquired_date": "2026-01-15",
                                         "cost": "2400000"}, follow_redirects=False)
    aid = int(r.headers["location"].split("/")[3].split("?")[0])
    from app.modules.ga import repo, services
    a = repo.get("ga_assets", aid)
    assert re.fullmatch(r"IT-2026-\d{3}", a["asset_no"]) and a["useful_years"] == 4
    dep = services.depreciation(a, date(2026, 12, 31))   # 12개월 / 48개월
    assert dep["accumulated"] == 600000 and dep["book"] == 1800000
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "지급", "event_date": "2026-02-01", "holder_id": "1"})
    assert repo.get("ga_assets", aid)["holder_id"] == 1
    ga.post(f"/ga/assets/{aid}/event", data={"event_type": "반납", "event_date": "2026-03-01"})
    a = repo.get("ga_assets", aid)
    assert a["holder_id"] is None and a["status"] == "보관"
    assert len(repo.asset_history(aid)) == 3


def test_resigned_holder_alert(ga):
    from app.modules.ga import services
    alerts = services.dashboard()["alerts"]
    assert any(a["category"] == "자산회수" for a in alerts)   # 샘플: 퇴사자 구민재 노트북


def test_vehicle_log_validation_and_logbook(ga):
    r = ga.post("/ga/vlogs/save", data={"vehicle_id": 1, "drive_date": "2026-11-01", "driver_id": 1, "purpose": "일반업무",
                                        "km_start": "90000", "km_end": "89000"}, follow_redirects=False)
    assert "%EB%8F%84%EC%B0%A9" in r.headers["location"]  # '도착' 오류 메시지
    csv = ga.get("/ga/vehicles/1/logbook.csv?year=2026").text
    assert "업무용승용차 운행기록부" in csv and "업무사용비율" in csv
    raw = sqlite3.connect(settings.db_path).execute("SELECT destination FROM ga_vehicle_logs LIMIT 1").fetchone()[0]
    assert raw.startswith("enc:v1:")   # 행선지 암호화


def test_doc_numbering(ga):
    from app.modules.ga import repo
    n1 = repo.next_doc_no("발신", "2026-05-01", "총무")
    ga.post("/ga/docs/save", data={"direction": "발신", "doc_date": "2026-05-01", "counterpart": "테스트기관", "title": "t"})
    n2 = repo.next_doc_no("발신", "2026-05-01", "총무")
    assert n1.startswith("총무-2026-") and int(n2[-4:]) == int(n1[-4:]) + 1
    assert repo.next_doc_no("수신", "2026-05-01", "총무").startswith("수신-2026-")


def test_seal_approval_admin_only(ga, admin):
    ga.post("/ga/seals/save", data={"use_date": "2026-11-05", "seal_type": "법인인감", "purpose": "테스트", "requester_id": 1})
    from app.modules.ga import repo
    sid = repo.list_seals(status="신청")[0]["id"]
    assert ga.post(f"/ga/seals/{sid}/decide", data={"decision": "승인"}).status_code == 403
    admin.post(f"/ga/seals/{sid}/decide", data={"decision": "승인"})
    assert repo.get("ga_seal_logs", sid)["status"] == "승인"


def test_contract_monthly_and_notice(ga):
    from app.modules.ga import services
    c = services.contract_view({"amount": 1200000, "pay_cycle": "연", "end_date": "2099-12-31", "notice_days": 30,
                                "status": "유효", "auto_renew": 1})
    assert c["monthly"] == 100000 and c["notice_date"] == "2099-12-01" and c["phase"] == "유효"


def test_event_rule_autofill(ga):
    ga.post("/ga/events/save", data={"employee_id": 1, "category": "결혼", "relation": "본인", "event_date": "2026-10-10",
                                     "status": "신청"})
    from app.modules.ga import repo
    e = next(x for x in repo.list_events(year=2026) if x["event_date"] == "2026-10-10" and x["employee_id"] == 1)
    assert e["amount"] == 500000 and e["leave_days"] == 5


def test_permissions():
    with TestClient(app):
        ga = _client("ga_staff2", "ga")
        assert ga.get("/hr/salaries").status_code == 403            # 총무담당: 연봉 불가
        assert "🔒" in ga.get("/hr/employees/1").text                # 개인정보 마스킹
        v = _client("ga_viewer", "viewer")
        assert v.get("/ga/assets").status_code == 200
        assert v.get("/ga/events").status_code == 403               # 경조사(가족정보) 비공개
        assert v.post("/ga/assets/save", data={"category": "기타", "name": "x"}).status_code == 403
        assert "자산 등록" not in v.get("/ga/assets").text


def test_users_role_migration(tmp_path):
    """v2 DB(users.role CHECK 에 'ga' 없음)도 자동 변환."""
    from app.db import _migrate_users_role
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT NOT NULL CHECK(role IN ('admin','hr','viewer')))")
    conn.execute("INSERT INTO users VALUES (1, 'a', 'admin')")
    _migrate_users_role(conn)
    conn.execute("INSERT INTO users VALUES (2, 'b', 'ga')")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
