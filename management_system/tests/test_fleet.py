"""법인차량 관리 강화: 사용자 이력, 사진 점검(촬영·비교·확정·잠금), 차량 QR 본인확인, 사고·파손, 과태료, 비용·알림."""
import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import execute, query, query_one
from app.main import app
from app.modules.ga import fleet, services
from app.modules.hr import repo as hr
from app.security import auth

O = {"origin": "http://testserver"}


def _jpg(color=(200, 30, 30)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(b, "JPEG")
    return b.getvalue()


def _client(username, role):
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
        auth.create_user(username, username, role, "Fl-Strong-Pass-1!", must_change=False)
    c.post("/login", data={"username": username, "password": "Fl-Strong-Pass-1!"}, headers=O)
    c.headers.update(O)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
    return c


@pytest.fixture(scope="module")
def env():
    with TestClient(app):
        staff = _client("fleet_ga", "ga")
        vid = execute("INSERT INTO ga_vehicles(plate_no, model, ownership, monthly_cost, status, acquired_date) "
                      "VALUES ('88허 8888', '테스트카', '리스', 500000, '운행', '2026-01-01')")
        a = hr.create_employee({"name": "인계김", "hire_date": "2020-01-01", "birth_date": "1990-01-01", "department": "영업팀"})
        b = hr.create_employee({"name": "인수박", "hire_date": "2021-01-01", "birth_date": "1992-02-02", "department": "영업팀"})
        c = hr.create_employee({"name": "남이최", "hire_date": "2021-01-01", "birth_date": "1993-03-03", "department": "공무팀"})
        yield {"staff": staff, "vid": vid, "a": a, "b": b, "c": c}


def _shoot_all(staff, vid, iid, damage_note=None):
    for k, _ in fleet.SLOTS:
        r = staff.post(f"/ga/vehicles/{vid}/inspections/{iid}/photo", data={"slot": k}, files={"file": ("p.jpg", _jpg())})
        assert r.status_code == 200 and r.json()["ok"], r.text
    if damage_note:
        r = staff.post(f"/ga/vehicles/{vid}/inspections/{iid}/photo", data={"slot": "damage", "note": damage_note},
                       files={"file": ("d.jpg", _jpg((10, 10, 10)))})
        assert r.json()["ok"]


def test_handover_flow_and_lock(env):
    s, vid, a, b = env["staff"], env["vid"], env["a"], env["b"]
    fleet.set_user(vid, a, "2026-03-01", "최초 배정")
    assert fleet.current_user(vid)["employee_id"] == a
    # 신차인수 기준 점검(확정) 하나 만들기
    iid0, err = fleet.create_inspection(vid, "정기점검", "2026-03-01", by="관리부")
    _shoot_all(s, vid, iid0)
    s.post(f"/ga/vehicles/{vid}/inspections/{iid0}/finish")
    s.post(f"/ga/vehicles/{vid}/inspections/{iid0}/finalize", data={})
    assert fleet.get_inspection(iid0)["status"] == "확정"
    # 인수자 없이 사용자변경 → 거절
    r = s.post(f"/ga/vehicles/{vid}/inspections", data={"kind": "사용자변경", "insp_date": "2026-11-10"}, follow_redirects=False)
    assert "%EC%9D%B8%EC%88%98%EC%9E%90" in r.headers["location"]          # '인수자'
    r = s.post(f"/ga/vehicles/{vid}/inspections", data={"kind": "사용자변경", "insp_date": "2026-11-10", "to_emp_id": b},
               follow_redirects=False)
    iid = int(re.search(r"/inspections/(\d+)", r.headers["location"]).group(1))
    i = fleet.get_inspection(iid)
    assert i["from_emp_id"] == a and i["prev_id"] == iid0
    # 다 찍기 전에는 촬영 완료 불가
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/photo", data={"slot": "front"}, files={"file": ("p.jpg", _jpg())})
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/finish")
    assert fleet.get_inspection(iid)["status"] == "촬영중"
    _shoot_all(s, vid, iid, damage_note="뒤 범퍼 긁힘")
    assert len(fleet.slot_map(iid)) == 8                                     # 다시 찍으면 바뀜(중복 없음)
    # 잘못된 파일
    r = s.post(f"/ga/vehicles/{vid}/inspections/{iid}/photo", data={"slot": "rear"}, files={"file": ("x.jpg", b"nope")})
    assert r.status_code == 400
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/finish")
    assert fleet.get_inspection(iid)["status"] == "확인대기"
    page = s.get(f"/ga/vehicles/{vid}/inspections/{iid}").text
    assert "사진 비교" in page and "인계김" in page and "관리부 입회 확인" in page
    # 운행기록 → 책임 후보
    execute("INSERT INTO ga_vehicle_logs(vehicle_id, drive_date, driver_id, purpose, km_start, km_end) VALUES (?,?,?,?,?,?)",
            (vid, "2026-06-01", env["c"], "일반업무", 1000, 1050))
    cands = {c["employee_id"] for c in fleet.drivers_between(vid, "2026-03-01", "2026-11-10")}
    assert {a, env["c"]} <= cands
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/confirm", data={"side": "from"})
    assert "입회" in fleet.get_inspection(iid)["from_ok_by"]
    # 새 파손인데 책임 대상 없음 → 거절
    rear = fleet.slot_map(iid)["rear"]["id"]
    r = s.post(f"/ga/vehicles/{vid}/inspections/{iid}/finalize", data={f"verdict_{rear}": "새파손"}, follow_redirects=False)
    assert fleet.get_inspection(iid)["status"] == "확인대기"
    dmg = next(p for p in fleet.photo_rows(iid) if p["slot"] == "damage")
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/finalize",
           data={f"verdict_{rear}": "새파손", f"note_{rear}": "뒤 범퍼 긁힘 10cm", f"verdict_{dmg['id']}": "기존파손",
                 "responsible_id": str(a)})
    i = fleet.get_inspection(iid)
    assert i["status"] == "확정" and i["result"] == "새파손" and i["responsible_id"] == a
    d = query("SELECT * FROM ga_vehicle_damages WHERE inspection_id=?", (iid,))
    assert len(d) == 1 and d[0]["driver_id"] == a and d[0]["part"] == "뒤"
    assert fleet.current_user(vid)["employee_id"] == b                         # 사용자 변경 반영
    assert any(h["employee_id"] == a and h["end_date"] == "2026-11-09" for h in fleet.assign_history(vid))
    # 잠금: 확정 후 사진 추가·삭제·점검 삭제 불가
    r = s.post(f"/ga/vehicles/{vid}/inspections/{iid}/photo", data={"slot": "front"}, files={"file": ("p.jpg", _jpg())})
    assert r.status_code == 400
    assert s.post(f"/ga/vehicles/{vid}/inspections/{iid}/photo/{rear}/delete").status_code == 400
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/delete")
    assert fleet.get_inspection(iid)
    # 사진 파일과 지문
    p = query_one("SELECT * FROM ga_vehicle_photos WHERE id=?", (rear,))
    img = s.get(f"/ga/vehicle-photos/{rear}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    import hashlib
    assert hashlib.sha256(img.content).hexdigest() == p["sha256"]
    assert s.get(f"/ga/vehicle-photos/{rear}?thumb=1").status_code == 200


def test_qr_employee_flow(env):
    s, vid, b, c = env["staff"], env["vid"], env["b"], env["c"]
    token = fleet.qr_token(vid)
    pub = TestClient(app)
    pub.headers.update(O)
    assert "본인 확인" in pub.get(f"/vq/{token}").text
    assert pub.get("/vq/wrong-token").status_code == 404
    r = pub.post(f"/vq/{token}/verify", data={"name": "인수박", "birth": "000000"}, follow_redirects=False)
    assert "e=" in r.headers["location"]
    r = pub.post(f"/vq/{token}/verify", data={"name": "인수박", "birth": "920202"}, follow_redirects=False)
    assert r.status_code == 303 and "vq_s" in r.headers.get("set-cookie", "")
    home = pub.get(f"/vq/{token}").text
    assert "인수박" in home and "파손·고장 신고" in home
    # 파손 신고
    r = pub.post(f"/vq/{token}/start", data={"kind": "파손신고"}, follow_redirects=False)
    iid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "파손 부위" in pub.get(f"/vq/{token}/i/{iid}").text
    pub.post(f"/vq/{token}/i/{iid}/finish")
    assert fleet.get_inspection(iid)["status"] == "촬영중"                     # 사진 없이는 완료 안 됨
    r = pub.post(f"/vq/{token}/i/{iid}/photo", data={"slot": "damage", "note": "사이드미러 깨짐"}, files={"file": ("d.jpg", _jpg())})
    assert r.json()["ok"]
    pub.post(f"/vq/{token}/i/{iid}/finish")
    assert fleet.get_inspection(iid)["status"] == "확인대기"
    pid = fleet.photo_rows(iid)[0]["id"]
    assert pub.get(f"/vq/{token}/p/{pid}?thumb=1").status_code == 200
    assert any(a["category"] == "차량점검" and a["name"] == "88허 8888" for a in fleet.alerts())
    # 다른 직원은 남의 점검을 못 엶
    other = TestClient(app)
    other.headers.update(O)
    other.post(f"/vq/{token}/verify", data={"name": "남이최", "birth": "930303"})
    assert other.get(f"/vq/{token}/i/{iid}").status_code == 403
    # 로그인 안 한 사람은 사진 못 봄
    anon = TestClient(app)
    assert anon.get(f"/vq/{token}/p/{pid}").status_code == 403
    assert anon.get(f"/ga/vehicle-photos/{pid}", follow_redirects=False).status_code in (303, 401)
    # 관리부 확정 → 파손 기록 (신고는 기본 '새파손'), 책임 미정 허용
    s.post(f"/ga/vehicles/{vid}/inspections/{iid}/finalize", data={"responsible_id": "-1"})
    d = query_one("SELECT * FROM ga_vehicle_damages WHERE inspection_id=?", (iid,))
    assert d and d["driver_id"] is None and d["detail"] == "사이드미러 깨짐"
    # 사용자변경 점검: 인수자 본인 휴대폰 확인
    iid2, err = fleet.create_inspection(vid, "반납", "2026-11-11", by="관리부")
    assert not err and fleet.get_inspection(iid2)["from_emp_id"] == b
    assert "반납" in pub.get(f"/vq/{token}").text
    for k, _ in fleet.SLOTS:
        pub.post(f"/vq/{token}/i/{iid2}/photo", data={"slot": k}, files={"file": ("p.jpg", _jpg())})
    pub.post(f"/vq/{token}/i/{iid2}/finish")
    assert "확인합니다" in pub.get(f"/vq/{token}/i/{iid2}").text
    pub.post(f"/vq/{token}/i/{iid2}/confirm", data={"side": "from"})
    assert "본인 휴대폰" in fleet.get_inspection(iid2)["from_ok_by"]
    assert pub.post(f"/vq/{token}/i/{iid2}/confirm", data={"side": "to"}).status_code == 403
    s.post(f"/ga/vehicles/{vid}/inspections/{iid2}/finalize", data={})
    assert fleet.current_user(vid) is None                                     # 반납


def test_qr_lockout(env):
    vid = env["vid"]
    for _ in range(fleet.MAX_FAILS):
        fleet.verify_person(vid, "인계김", "111111")
    emp, err = fleet.verify_person(vid, "인계김", "900101")
    assert emp is None and "잠겼" in err
    fleet._fails.pop(vid, None)
    assert fleet.verify_person(vid, "인계김", "900101")[0]


def test_fines_and_damages(env):
    s, vid, c = env["staff"], env["vid"], env["c"]
    r = s.post("/ga/fines/save", data={"vehicle_id": vid, "violation_at": "2026-06-01T14:20", "kind": "속도위반",
                                        "amount": "40000", "due_date": "2026-11-15"}, follow_redirects=False)
    assert r.status_code == 303
    f = query_one("SELECT * FROM ga_vehicle_fines WHERE vehicle_id=? ORDER BY id DESC", (vid,))
    assert f["driver_id"] == c and f["driver_source"] == "운행기록"
    assert s.get(f"/ga/fines/driver?vehicle_id={vid}&at=2026-06-01T10:00").json()["driver_id"] == c
    # 운행기록 없는 날 → 그날 사용자
    r = s.post("/ga/fines/save", data={"vehicle_id": vid, "violation_at": "2026-05-01 09:00", "kind": "주정차위반", "amount": "40000"})
    f2 = query_one("SELECT * FROM ga_vehicle_fines WHERE vehicle_id=? ORDER BY id DESC", (vid,))
    assert f2["driver_id"] == env["a"] and f2["driver_source"] == "사용자"
    assert any(a["category"] == "과태료" for a in fleet.alerts())
    s.post("/ga/fines/save", data={"id": f2["id"], "vehicle_id": vid, "violation_at": "2026-05-01 09:00", "kind": "주정차위반",
                                   "amount": "40000", "driver_id": env["a"], "status": "급여공제"})
    assert query_one("SELECT * FROM ga_vehicle_fines WHERE id=?", (f2["id"],))["paid_date"]
    s.post("/ga/damages/save", data={"vehicle_id": vid, "occur_date": "2026-07-01", "kind": "사고", "repair_cost": "500000",
                                     "self_pay": "900000", "insurance": "회사부담", "responsibility": "운전자", "driver_id": c})
    d = query_one("SELECT * FROM ga_vehicle_damages WHERE vehicle_id=? AND kind='사고'", (vid,))
    assert d["self_pay"] == 500000                                             # 본인 부담은 수리비를 넘지 않음
    cost = fleet.cost_table(2026)
    row = next(r for r in cost["rows"] if r["id"] == vid)
    assert row["lease"] == 500000 * row["months"] and row["damage"] == 0 and row["fine_cnt"] == 2
    s.post(f"/ga/damages/{d['id']}/delete", data={"next": "/ga/vehicles?tab=damages"})
    assert not query_one("SELECT 1 x FROM ga_vehicle_damages WHERE id=?", (d["id"],))


def test_oil_alert(env):
    s, vid = env["staff"], env["vid"]
    execute("INSERT INTO ga_vehicle_maint(vehicle_id, maint_date, category, detail, km, cost) VALUES (?,?,?,?,?,?)",
            (vid, "2026-01-10", "소모품", "엔진오일 교체", 100, 80000))
    s.post(f"/ga/vehicles/{vid}/oil", data={"oil_interval_km": "1000"})
    o = fleet.oil_status(fleet.repo.get("ga_vehicles", vid))
    assert o["interval"] == 1000 and o["left"] <= 1000 and o["next_km"] == 1100
    assert any(a["category"] == "차량정비" and a["name"] == "88허 8888" for a in services.build_alerts([], fleet.repo.list_vehicles(), []))


def test_pages_and_permissions(env):
    s, vid = env["staff"], env["vid"]
    for url in ["/ga/vehicles", "/ga/vehicles?tab=inspections", "/ga/vehicles?tab=damages&status=진행", "/ga/vehicles?tab=fines",
                "/ga/vehicles?tab=cost", f"/ga/vehicles/{vid}", "/ga/vehicles/labels", f"/ga/vehicles/labels?ids={vid}",
                "/ga/dashboard"]:
        r = s.get(url)
        assert r.status_code == 200, url
    assert "/vq/" in s.get("/ga/vehicles/labels").text
    assert "현재 사용자" in s.get(f"/ga/vehicles/{vid}").text
    iid = fleet.inspections(vid)[0]["id"]
    assert s.get(f"/ga/vehicles/{vid}/inspections/{iid}/shoot").status_code == 200
    assert "관리부 담당자로 로그인" in s.get(f"/vq/{fleet.qr_token(vid)}").text
    viewer = _client("fleet_view", "viewer")
    assert viewer.get(f"/ga/vehicles/{vid}").status_code == 200
    assert viewer.post("/ga/fines/save", data={"vehicle_id": vid, "violation_at": "2026-06-01 10:00"}).status_code == 403
    assert viewer.post(f"/ga/vehicles/{vid}/inspections", data={"kind": "정기점검"}).status_code == 403
    # 매각 차량 QR 은 열리지 않음
    execute("UPDATE ga_vehicles SET status='매각' WHERE id=?", (vid,))
    assert TestClient(app).get(f"/vq/{fleet.qr_token(vid)}").status_code == 404
    execute("UPDATE ga_vehicles SET status='운행' WHERE id=?", (vid,))
