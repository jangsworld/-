"""프로젝트 이중가입."""
import re
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.common import today
from app.config import settings
from app.db import execute, query_one
from app.main import app
from app.modules.hr import dual, ecount, repo, services
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
def hr():
    with TestClient(app):
        yield _client("dual_hr", "hr")


def _emp(name):
    execute("INSERT INTO employees(name, emp_no, department, hire_date) VALUES (?, ?, '시험팀', '2020-01-01')", (name, "DU-" + name))
    return query_one("SELECT id FROM employees WHERE name=?", (name,))["id"]


def test_dual_lifecycle(hr):
    t = today()
    a = _emp("이중가")
    repo.upsert_salary(a, t.year, 60_000_000)
    base = {"employee_id": str(a), "workplace": "A사업장", "project": "시험 구축사업", "client": "시험발주처",
            "start_date": (t - timedelta(days=60)).isoformat(), "end_date": (t + timedelta(days=20)).isoformat(),
            "reason": "발주처 요구", "reason_note": "상주 조건", "ins_health": "1", "ins_employ": "1",
            "pay_type": "조정", "our_before": "5,000,000", "our_after": "3,000,000", "other_pay": "2,000,000"}
    r = hr.post("/hr/dual/save", data=base, follow_redirects=False)
    assert r.status_code == 303
    row = dual.rows(employee_id=a)[0]
    assert row["state"] == "진행중" and row["ending"] and row["our_after"] == 3_000_000 and row["insurances"] == ["건강보험", "고용보험"]
    # 금액은 암호화 저장, 연봉은 그대로
    raw = sqlite3.connect(settings.db_path).execute("SELECT our_after FROM dual_enrollments WHERE id=?", (row["id"],)).fetchone()[0]
    assert "3000000" not in raw
    assert next(x for x in repo.employee_salaries(a) if x["year"] == t.year)["annual_salary"] == 60_000_000
    # 현황 화면·인사기록카드·급여 탭 안내·대시보드 알림
    page = hr.get("/hr/dual").text
    assert "A사업장" in page and "시험 구축사업" in page and "보험 금액에 조정액 넣기" in page
    card = hr.get(f"/hr/employees/{a}").text
    assert "이중가입 중 · A사업장" in card and "상주 조건" in card
    assert "두 곳의 근로소득을 합산" in hr.get(f"/hr/employees/{a}?tab=payroll").text
    assert any(x["category"] == "이중가입" and "원복 준비" in x["message"] for x in services.dashboard()["alerts"])
    # 처리 체크
    hr.post(f"/hr/dual/{row['id']}/check", data={"key": "s_report"})
    assert dual.get(row["id"])["checks"].get("s_report") and "우리 회사 보수월액 변경 신고" not in dual.get(row["id"])["todo_left"]
    hr.post(f"/hr/dual/{row['id']}/check", data={"key": "nope"})
    # 보험 기준 금액: 버튼을 눌렀을 때만 바뀜
    assert not ecount.get_payroll(a)["insured_monthly"]
    hr.post(f"/hr/dual/{row['id']}/apply-bases", data={"which": "after"})
    p = ecount.get_payroll(a)
    assert (p["insured_monthly"], p["pension_monthly"], p["employ_monthly"]) == (3_000_000,) * 3
    hr.post(f"/hr/dual/{row['id']}/apply-bases", data={"which": "before"})
    assert ecount.get_payroll(a)["insured_monthly"] == 5_000_000
    # 종료 처리 → 종료 상태, 남은 처리 표시
    hr.post("/hr/dual/save", data={**base, "id": str(row["id"]), "actual_end": t.isoformat()})
    row = dual.get(row["id"])
    assert row["state"] == "종료" and "우리 회사 보수월액 원복 신고" in row["todo_left"]
    assert [r["id"] for r in dual.rows("정리필요")] == [row["id"]]
    # 내려받기에는 금액이 없음
    csv_text = hr.get("/hr/dual/export.csv").text
    assert "A사업장" in csv_text and "3000000" not in csv_text and "3,000,000" not in csv_text
    hr.post(f"/hr/dual/{row['id']}/delete")
    assert not dual.rows(employee_id=a)


def test_dual_validation_and_same_pay(hr):
    b = _emp("이중나")
    t = today().isoformat()
    bad = [{"workplace": "", "start_date": t}, {"workplace": "B", "start_date": ""},
           {"workplace": "B", "start_date": t, "end_date": "2000-01-01"},
           {"workplace": "B", "start_date": t, "pay_type": "조정", "our_before": "", "our_after": ""},
           {"workplace": "B", "start_date": t, "reason": "없는사유"}]
    for d in bad:
        hr.post("/hr/dual/save", data={"employee_id": str(b), **d})
    assert not dual.rows(employee_id=b)
    hr.post("/hr/dual/save", data={"employee_id": str(b), "workplace": "B사업장", "start_date": t, "pay_type": "그대로",
                                   "our_after": "999"})
    row = dual.rows(employee_id=b)[0]
    assert row["pay_type"] == "그대로" and row["our_after"] is None and [n for _k, n in row["todo_start"]] == ["상대 사업장 4대보험 취득 확인"]
    r = hr.post(f"/hr/dual/{row['id']}/apply-bases", data={"which": "after"}, follow_redirects=False)
    assert not ecount.get_payroll(b)["insured_monthly"]


def test_dual_technician_badge_and_permission(hr):
    c = _emp("이중기술")
    repo.save_qualifications(c, [{"kind": repo.QUALIFICATION_KINDS[0] if hasattr(repo, "QUALIFICATION_KINDS") else "정보통신공사기술자", "grade": "초급"}]) \
        if hasattr(repo, "save_qualifications") else None
    hr.post("/hr/dual/save", data={"employee_id": str(c), "workplace": "C사업장", "start_date": today().isoformat()})
    if hasattr(repo, "save_qualifications"):
        assert "이중가입 · C사업장" in hr.get("/hr/technicians").text
    viewer = _client("dual_view", "viewer")
    assert viewer.get("/hr/dual").status_code == 403
    assert viewer.post("/hr/dual/save", data={"employee_id": str(c), "workplace": "X", "start_date": "2026-01-01"}).status_code == 403
    assert "이중가입 중" not in viewer.get(f"/hr/employees/{c}").text
    assert TestClient(app).get("/hr/dual", follow_redirects=False).status_code in (303, 401)


def test_dashboard_dual_card_dept_top_level_and_hire_route(hr):
    from app.db import dept_tree, get_settings, set_setting
    admin = _client("dual_admin", "admin")
    # 부서 3단계: 그래프는 대분류로 합산
    admin.post("/settings/codes", data={"group_code": "DEPT", "name": "그래프본부"})
    pid = query_one("SELECT id FROM codes WHERE name='그래프본부'")["id"]
    admin.post("/settings/codes", data={"group_code": "DEPT", "name": "그래프팀", "parent_id": str(pid)})
    e1 = _emp("그래프직원")
    execute("UPDATE employees SET department='그래프팀' WHERE id=?", (e1,))
    d = services.dashboard()
    labels = {x["label"]: x["count"] for x in d["charts"]["dept"]} if "charts" in d else {x["label"]: x["count"] for x in d["dept"]}
    assert labels.get("그래프본부") == 1 and "그래프팀" not in labels
    # 이중가입 현황 카드
    hr.post("/hr/dual/save", data={"employee_id": str(e1), "workplace": "D사업장", "project": "그래프 사업",
                                   "start_date": (today() - timedelta(days=3)).isoformat()})
    page = hr.get("/hr/dashboard").text
    assert "타 사업장 이중가입" in page and "D사업장" in page.split('id="dualnow"')[1].split("</table>")[0]
    # 채용경로: 고용형태의 '(소개)'를 한 번 옮김
    e2 = _emp("소개직원")
    execute("UPDATE employees SET employment_type='정규직(소개)' WHERE id=?", (e2,))
    execute("INSERT OR IGNORE INTO codes(group_code, name, sort_order) VALUES ('EMPLOYMENT_TYPE', '정규직(소개)', 99)")
    execute("DELETE FROM settings WHERE key='hire_route_split_v1'")
    assert repo.split_hire_route_once() == 1 and repo.split_hire_route_once() == 0
    e = repo.get_employee(e2)
    assert e["employment_type"] == "정규직" and e["hire_route"] == "소개"
    assert not query_one("SELECT 1 x FROM codes WHERE name='정규직(소개)'")
    raw = sqlite3.connect(settings.db_path).execute("SELECT hire_route FROM employees WHERE id=?", (e2,)).fetchone()[0]
    assert raw.startswith("enc:")
    form = hr.get(f"/hr/employees/{e2}/edit").text
    assert 'name="hire_route" value="소개"' in form
    assert "채용경로" in hr.get(f"/hr/employees/{e2}").text
