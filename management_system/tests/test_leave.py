"""연차 관리: 발생 계산(회계연도), 메일플러그 휴가 내역 올리기, 1차 촉진·직원 회신, 2차 지정."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import mailer, storage
from app.config import settings
from app.db import execute, query, query_one
from app.main import app
from app.modules.hr import leave, repo
from app.security import auth

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='leave_hr'"):
            auth.create_user("leave_hr", "인사", "hr", "Strong-Pass-123!", must_change=False)
            auth.create_user("leave_view", "조회", "viewer", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "leave_hr", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setenv("BMS_TODAY", "2026-07-02")


class FakeMail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, body, attachments=None):
        self.sent.append({"to": to, "subject": subject, "body": body, "attachments": attachments or []})


@pytest.fixture
def mail(monkeypatch, tmp_path):
    fake = FakeMail()
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", fake)
    monkeypatch.setattr(storage, "render_pdf", lambda content, title: b"%PDF-1.7 " + content.encode()[:60])
    monkeypatch.setattr(settings, "employee_root", str(tmp_path))
    return fake


# ── 계산 ────────────────────────────────────────────────────────────────
def test_parse_days():
    assert leave.parse_days("1d (8h)", "1d (8h)") == 1
    assert leave.parse_days("오후 (4h)", "4h") == 0.5
    assert leave.parse_days("오전 (5.33h)", "5.33h") == 0.5
    assert leave.parse_days("1d (10.66h)", "1d (10.66h)") == 1
    assert leave.parse_days("3d (24h)", "3d (24h)") == 3
    assert leave.parse_days("1d", "1d") == 1
    assert leave.parse_days("", "2h") == 0.25
    with pytest.raises(ValueError):
        leave.parse_days("", "")


def test_entitlement_fiscal_year():
    assert leave.entitlement("2020-03-01", 2026)["annual"] == 17          # 6년차: 15 + (6-1)//2
    assert leave.entitlement("2024-01-01", 2026)["annual"] == 15
    assert leave.entitlement("2023-05-01", 2026)["annual"] == 16
    assert leave.entitlement("1980-01-01", 2026)["annual"] == 25          # 최대 25일
    e = leave.entitlement("2025-07-01", 2026)                            # 입사 다음 해: 비례 + 남은 월차
    assert e["annual"] == 7.6 and e["monthly"] == 6 and e["monthly_expires"] == "2026-07-01"
    e = leave.entitlement("2026-01-01", 2026)                            # 입사 연도: 월차만 (오늘 7/2 까지 6개)
    assert e["annual"] == 0 and e["monthly"] == 6 and e["first_year"]
    assert leave.entitlement("2027-01-01", 2026)["annual"] == 0
    assert leave.entitlement("2020-03-01", 2026, resign="2025-12-31")["annual"] == 0


# ── 올리기 ──────────────────────────────────────────────────────────────
def _emp(email=True):
    e = next(e for e in repo.list_employees(status="재직(전체)") if e.get("email") and e.get("birth_date") and
             e["hire_date"] < "2024-01-01")
    return e


def _row(doc, name, start, end=None, time="1d (8h)", typ="연차", status="승인"):
    return {"line": 2, "doc_no": doc, "name": name, "rank": "과장", "dept": "팀", "type": typ, "status": status,
            "time": time, "start": start, "end": end or start, "days": leave.parse_days(time, time)}


def test_import_dedup_cancel_and_link(client, monkeypatch):
    emp = _emp()
    files = {
        "Vacation_history_2026-05-01_2026-05-31.xls": [
            _row("휴가-T-1", emp["name"], "2026-05-11"),
            _row("휴가-T-2", emp["name"], "2026-05-29", "2026-06-01", "2d (16h)"),
            _row("휴가-T-3", "외부 사람(J)", "2026-05-12", time="오후 (4h)"),
            _row("휴가-T-4", emp["name"], "2026-05-13", typ="보건휴가(유급)")],
        "Vacation_history_2026-06-01_2026-06-30.xls": [
            _row("휴가-T-2", emp["name"], "2026-05-29", "2026-06-01", "2d (16h)"),
            _row("휴가-T-5", emp["name"], "2026-06-15", time="오전 (4h)")],
    }
    monkeypatch.setattr(leave, "read_rows", lambda path, name: list(files[name]))
    r = client.post("/hr/leave/import", files=[("files", (n, b"x", "application/vnd.ms-excel")) for n in files],
                    follow_redirects=False)
    assert r.status_code == 303
    recs = query("SELECT * FROM leave_records WHERE doc_no LIKE '휴가-T-%' ORDER BY doc_no")
    assert [x["doc_no"] for x in recs] == ["휴가-T-1", "휴가-T-2", "휴가-T-3", "휴가-T-5"]   # 중복 1건, 보건휴가 제외
    assert not query_one("SELECT 1 x FROM leave_records WHERE leave_type LIKE '%보건%'")
    assert leave.covered_months(2026) == [5, 6]
    assert any(u["mp_name"] == "외부 사람(J)" for u in leave.unmatched())
    # 같은 달 다시 올림: 취소된 T-1 은 빠짐
    files["Vacation_history_2026-05-01_2026-05-31.xls"] = files["Vacation_history_2026-05-01_2026-05-31.xls"][1:]
    client.post("/hr/leave/import", files=[("files", ("Vacation_history_2026-05-01_2026-05-31.xls", b"x", "x"))])
    assert not query_one("SELECT 1 x FROM leave_records WHERE doc_no='휴가-T-1'")
    # 이름 연결
    client.post("/hr/leave/link", data={"mp_name": "외부 사람(J)", "employee_id": emp["id"]})
    assert query_one("SELECT employee_id FROM leave_records WHERE doc_no='휴가-T-3'")["employee_id"] == emp["id"]
    s = leave.summary(repo.get_employee(emp["id"]), 2026)
    assert s["used"] == 2 + 0.5 + 0.5
    page = client.get("/hr/leave?year=2026").text
    assert emp["name"] in page and "5월" in page
    assert "휴가-T-2" in client.get(f"/hr/leave/{emp['id']}?year=2026").text


def test_import_rejects_bad_file(client, monkeypatch):
    def bad(path, name):
        raise leave.LeaveImportError("메일플러그 '휴가 내역' 엑셀이 아닙니다")
    monkeypatch.setattr(leave, "read_rows", bad)
    r = client.post("/hr/leave/import", files=[("files", ("x.xls", b"x", "x"))], follow_redirects=False)
    assert "%ED%9C%B4%EA%B0%80" in r.headers["location"] or "msg=" in r.headers["location"]


def test_adjust(client):
    emp = _emp()
    before = leave.summary(repo.get_employee(emp["id"]), 2026)
    client.post(f"/hr/leave/{emp['id']}/adjust", data={"year": 2026, "days": "2", "used_before": "3", "note": "이월"})
    after = leave.summary(repo.get_employee(emp["id"]), 2026)
    assert after["granted"] == before["granted"] + 2 and after["used"] == before["used"] + 3


# ── 촉진 ────────────────────────────────────────────────────────────────
def test_first_promotion_and_employee_reply(client, mail):
    emp = _emp()
    client.post("/hr/leave/promote", data={"year": 2026, "emp_ids": [emp["id"]]})
    assert mail.sent and mail.sent[-1]["to"] == emp["email"] and "/leave-plan/" in mail.sent[-1]["body"]
    assert mail.sent[-1]["attachments"]                                       # 통보서 PDF 첨부
    p = query_one("SELECT * FROM leave_promotions WHERE employee_id=? AND round=1 ORDER BY id DESC", (emp["id"],))
    assert p["status"] == "회신대기" and p["deadline"] == "2026-07-12" and p["doc_id"]
    token = re.search(r"/leave-plan/(\S+)", mail.sent[-1]["body"]).group(1)

    anon = TestClient(app)
    anon.headers.update(ORIGIN)
    assert "본인 확인" in anon.get(f"/leave-plan/{token}").text
    anon.post(f"/leave-plan/{token}/verify", data={"birth": "000000"})
    assert query_one("SELECT fail_count FROM leave_promotions WHERE id=?", (p["id"],))["fail_count"] == 1
    birth = emp["birth_date"].replace("-", "")[2:]
    anon.post(f"/leave-plan/{token}/verify", data={"birth": birth})
    assert "연차 사용 날짜" in anon.get(f"/leave-plan/{token}").text
    # 주말·잔여 초과는 거부
    r = anon.post(f"/leave-plan/{token}/submit", data={"plan_date": ["2026-07-04"], "plan_part": ["종일"]}, follow_redirects=False)
    assert "e=" in r.headers["location"]
    r = anon.post(f"/leave-plan/{token}/submit", data={"plan_date": ["2026-08-03"] * 2, "plan_part": ["종일"] * 2},
                  follow_redirects=False)
    assert "e=" in r.headers["location"]
    r = anon.post(f"/leave-plan/{token}/submit", data={"plan_date": ["2026-08-03", "2026-08-04"], "plan_part": ["종일", "오후"]})
    assert "접수" in r.text
    plans = leave.plans(emp["id"], 2026)
    assert [(x["plan_date"], x["part"], x["source"]) for x in plans] == [("2026-08-03", "종일", "직원회신"), ("2026-08-04", "오후", "직원회신")]
    assert query_one("SELECT status FROM leave_promotions WHERE id=?", (p["id"],))["status"] == "회신완료"
    assert query_one("SELECT 1 x FROM hr_documents WHERE employee_id=? AND category='연차사용계획'", (emp["id"],))
    s = leave.summary(repo.get_employee(emp["id"]), 2026)
    assert s["upcoming"] == 1.5 and s["promo"]["label"] == "1차 회신완료"
    # 기한 지나면 링크 막힘
    execute("UPDATE leave_promotions SET deadline='2026-07-01' WHERE id=?", (p["id"],))
    assert "기한이 지났습니다" in anon.get(f"/leave-plan/{token}").text


def test_second_designation(client, mail):
    emp = _emp()
    n = len(mail.sent)
    client.post(f"/hr/leave/{emp['id']}/designate", data={"year": 2026, "plan_date": ["2026-09-07", "2026-09-08"],
                                                          "plan_part": ["종일", "오전"]})
    assert len(mail.sent) == n + 1 and "2026-09-07" in mail.sent[-1]["body"]
    assert {x["plan_date"] for x in leave.plans(emp["id"], 2026) if x["source"] == "회사지정"} == {"2026-09-07", "2026-09-08"}
    assert leave.summary(repo.get_employee(emp["id"]), 2026)["promo"]["label"] == "2차 지정통보"
    # 지난 날짜·주말 거부
    client.post(f"/hr/leave/{emp['id']}/designate", data={"year": 2026, "plan_date": ["2026-06-01"], "plan_part": ["종일"]})
    client.post(f"/hr/leave/{emp['id']}/designate", data={"year": 2026, "plan_date": ["2026-09-05"], "plan_part": ["종일"]})
    assert len(mail.sent) == n + 1


def test_viewer_cannot_open_leave():
    with TestClient(app) as c:
        c.post("/login", data={"username": "leave_view", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        assert c.get("/hr/leave", follow_redirects=False).status_code in (403, 303)
        assert "연차 관리" not in c.get("/hr/dashboard").text


def test_first_year_skipped_in_bulk_promotion(client, mail):
    from app.modules.hr.leave_routes import send_first
    eid = repo.create_employee({"name": "신입테스트", "hire_date": "2026-02-02", "email": "new@example.com",
                                "birth_date": "1999-01-01", "employment_type": "정규직"})
    res = send_first([eid], 2026, "http://testserver")
    assert not res["sent"] and "1년 미만" in res["skipped"][0]


# ── 명단 확인 후 일괄 발송 ──────────────────────────────────────────────
def test_review_first_round_with_edited_days(client, mail, monkeypatch):
    others = [e for e in repo.list_employees(status="재직(전체)") if e.get("email") and e.get("birth_date")
              and e["hire_date"] < "2024-01-01" and not query_one(
                  "SELECT 1 x FROM leave_promotions WHERE employee_id=? AND year=2026 AND status<>'취소'", (e["id"],))]
    assert len(others) >= 2
    page = client.get("/hr/leave/review?year=2026&round=1").text
    assert others[0]["name"] in page and "촉진 일수" in page
    assert "1차 연차 사용 촉진 기간" in client.get("/hr/dashboard").text         # 7/2: 발송 기간 알림
    a, b = others[0], others[1]
    n = len(mail.sent)
    r = client.post("/hr/leave/promote", data={"year": 2026, "from_review": "1", "emp_ids": [a["id"], b["id"]],
                                               f"days_{a['id']}": "7.5", f"days_{b['id']}": ""})
    assert len(mail.sent) == n + 2
    pa = query_one("SELECT remaining FROM leave_promotions WHERE employee_id=? AND round=1 AND status<>'취소'", (a["id"],))
    assert pa["remaining"] == 7.5                                                   # 담당자가 고친 일수로 통보
    assert any("7.5일" in m["body"] for m in mail.sent[n:])
    # 잘못된 일수면 아무에게도 안 보냄
    c = others[2] if len(others) > 2 else None
    if c:
        client.post("/hr/leave/promote", data={"year": 2026, "from_review": "1", "emp_ids": [c["id"]], f"days_{c['id']}": "abc"})
        assert len(mail.sent) == n + 2
    assert a["name"] not in client.get("/hr/leave/review?year=2026&round=1").text.split("<tbody>")[1]   # 보낸 사람은 명단에서 빠짐


def test_review_second_round_suggest_and_bulk(client, mail, monkeypatch):
    p = query_one("SELECT * FROM leave_promotions WHERE round=1 AND status='회신대기' AND year=2026 ORDER BY id LIMIT 1")
    assert p
    emp = repo.get_employee(p["employee_id"])
    monkeypatch.setenv("BMS_TODAY", "2026-07-20")                                  # 회신 기한(7/12) 지남
    rows = leave.second_candidates(2026)
    me = next(r for r in rows if r["emp"]["id"] == emp["id"])
    assert me["reason"] == "회신 없음" and me["days"] > 0
    dates = [d for d, _ in me["suggest"]]
    assert dates and all(d >= "2026-11-01" for d in dates) and "2026-12-25" not in dates
    assert all(date.fromisoformat(d).weekday() < 5 for d in dates)
    assert len(me["suggest"]) == int(-(-me["days"] // 1))
    assert "2차 사용 시기 지정 대상" in client.get("/hr/dashboard").text
    page = client.get("/hr/leave/review?year=2026&round=2").text
    assert emp["name"] in page and dates[0] in page
    n = len(mail.sent)
    # 날짜를 고쳐서 보냄 (형식 틀리면 아무에게도 안 보냄)
    client.post("/hr/leave/designate-bulk", data={"year": 2026, "emp_ids": [emp["id"]], f"dates_{emp['id']}": "11월3일"})
    assert len(mail.sent) == n
    client.post("/hr/leave/designate-bulk", data={"year": 2026, "emp_ids": [emp["id"]],
                                                   f"dates_{emp['id']}": "2026-11-03, 2026-11-04 오후"})
    assert len(mail.sent) == n + 1 and "2026-11-04 오후" in mail.sent[-1]["body"]
    assert {(x["plan_date"], x["part"]) for x in leave.plans(emp["id"], 2026) if x["source"] == "회사지정"} >= \
        {("2026-11-03", "종일"), ("2026-11-04", "오후")}
    assert all(r["emp"]["id"] != emp["id"] for r in leave.second_candidates(2026))


def test_parse_date_text():
    from app.modules.hr.leave_routes import parse_date_text
    rows, err = parse_date_text("2026-11-03, 2026-11-04 오전\n2026-11-05", 2026)
    assert not err and rows == [("2026-11-03", "종일"), ("2026-11-04", "오전"), ("2026-11-05", "종일")]
    assert parse_date_text("2026-11-07", 2026)[1]                  # 토요일
    assert parse_date_text("2026-11-03, 2026-11-03", 2026)[1]      # 중복


# ── 상해공제 ────────────────────────────────────────────────────────────
def test_injury_insurance_crud_and_alert(client, monkeypatch):
    monkeypatch.setenv("BMS_TODAY", "2026-11-11")
    emp = _emp()
    r = client.post(f"/hr/employees/{emp['id']}/insurances", data={
        "product": "근로자 상해공제", "insurer": "테스트공제조합", "joined_on": "2025-11-20", "start_date": "2025-11-20",
        "end_date": "2026-11-19", "policy_no": "P-001", "premium": "120,000", "note": ""}, follow_redirects=False)
    assert r.status_code == 303
    rows = repo.list_insurances(emp["id"])
    assert rows[0]["product"] == "근로자 상해공제" and rows[0]["premium"] == "120000"
    page = client.get(f"/hr/employees/{emp['id']}?tab=insurance").text
    assert "테스트공제조합" in page and "만기 D-8" in page
    from app.modules.hr import services
    alerts = services.build_alerts(repo.list_employees(status=""))
    assert any(a["category"] == "상해공제" and a["emp_id"] == emp["id"] for a in alerts)
    assert any(a["category"] == "상해공제" and a["emp_id"] is None for a in alerts)          # 미가입자 안내
    # 수정 · 잘못된 기간 거부 · 다른 직원 기록은 못 건드림
    client.post(f"/hr/employees/{emp['id']}/insurances", data={"ins_id": rows[0]["id"], "product": "근로자 상해공제",
                                                                "start_date": "2026-11-20", "end_date": "2027-11-19"})
    assert repo.list_insurances(emp["id"])[0]["end_date"] == "2027-11-19"
    client.post(f"/hr/employees/{emp['id']}/insurances", data={"product": "x", "start_date": "2026-05-01", "end_date": "2026-01-01"})
    assert len(repo.list_insurances(emp["id"])) == 1
    other = next(e for e in repo.list_employees(status="") if e["id"] != emp["id"])
    client.post(f"/hr/employees/{other['id']}/insurances/{rows[0]['id']}/delete")
    assert len(repo.list_insurances(emp["id"])) == 1
    client.post(f"/hr/employees/{emp['id']}/insurances/{rows[0]['id']}/delete")
    assert not repo.list_insurances(emp["id"])
