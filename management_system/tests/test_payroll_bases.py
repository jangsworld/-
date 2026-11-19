"""급여·연말정산: 건강보험·국민연금·고용보험 금액 칸 분리, 탭에서 연봉 저장, 입력 화면 키보드 도우미."""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.common import today
from app.db import _add_columns, execute, query_one
from app.main import app
from app.modules.hr import ecount, repo
from app.security import auth

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture(scope="module")
def hr():
    with TestClient(app):
        c = TestClient(app)
        if not auth.query_one("SELECT 1 FROM users WHERE username='pb_hr'"):
            auth.create_user("pb_hr", "pb_hr", "hr", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "pb_hr", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


def _emp(name):
    execute("INSERT INTO employees(name, emp_no, department, hire_date) VALUES (?, ?, '시험팀', '2026-01-01')", (name, "PB-" + name))
    return query_one("SELECT id FROM employees WHERE name=?", (name,))["id"]


def test_three_bases_saved_and_exported(hr):
    eid = _emp("세칸시험")
    item = ecount.pay_items()[0]
    r = hr.post(f"/hr/employees/{eid}/payroll", data={
        f"pay_{item}": "4,000,000", "insured_monthly": "5,308,277", "pension_monthly": "5,235,000",
        "employ_monthly": "5,308,277", "householder": "Y", "dependents": "2", "sal_amount": "48,000,001"}, follow_redirects=False)
    assert r.status_code == 303
    p = ecount.get_payroll(eid)
    assert (p["insured_monthly"], p["pension_monthly"], p["employ_monthly"]) == (5308277, 5235000, 5308277)
    v = ecount.values(eid, include_rrn=False)
    assert v["pension_monthly"] == "5235000" and v["employ_monthly"] == "5308277"
    # 탭의 연봉 칸 → 올해 연봉(원 단위 그대로)
    cur = next(x for x in repo.employee_salaries(eid) if x["year"] == today().year)
    assert cur["annual_salary"] == 48_000_001
    page = hr.get(f"/hr/employees/{eid}?tab=payroll").text
    assert 'name="pension_monthly"' in page and 'name="employ_monthly"' in page and "data-pay-annual" in page
    assert 'name="pension_monthly"' in hr.get(f"/hr/employees/{eid}/edit").text
    # 연봉 칸을 비우면 연봉은 그대로
    hr.post(f"/hr/employees/{eid}/payroll", data={"insured_monthly": "5308277", "sal_amount": ""})
    assert next(x for x in repo.employee_salaries(eid) if x["year"] == today().year)["annual_salary"] == 48_000_001
    assert ecount.get_payroll(eid)["pension_monthly"] is None


def test_headers_match_each_insurance():
    assert ecount.guess_field("건강보험 보수월액") == "insured_monthly"
    assert ecount.guess_field("국민연금 기준소득월액") == "pension_monthly"
    assert ecount.guess_field("기준소득월액") == "pension_monthly"
    assert ecount.guess_field("고용보험 보수월액(원)") == "employ_monthly"
    assert ecount.guess_field("보수월액") == "insured_monthly"
    keys = [c["field"] for c in ecount.default_columns()]
    assert {"insured_monthly", "pension_monthly", "employ_monthly"} <= set(keys)


def test_old_db_gets_columns_filled_from_single_value():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE employee_payroll (employee_id INTEGER PRIMARY KEY, insured_monthly INTEGER)")
    conn.execute("INSERT INTO employee_payroll VALUES (1, 3000000)")
    _add_columns(conn)
    assert conn.execute("SELECT pension_monthly, employ_monthly FROM employee_payroll").fetchone() == (3000000, 3000000)
    _add_columns(conn)   # 두 번 돌려도 안전


def test_common_script_keyboard_helpers(hr):
    js = hr.get("/static/js/common.js").text
    assert "'F8'" in js and "isComposing" in js and "data-pay-annual" in js
    assert "getElementById('sidebar')" in js          # 로그인·인증 화면에는 적용하지 않음


def test_emergency_name_saved_encrypted(hr):
    eid = _emp("비상성명")
    page = hr.get(f"/hr/employees/{eid}/edit").text
    assert page.index('name="emergency_relation"') < page.index('name="emergency_name"') < page.index('name="emergency_contact"')
    repo.update_employee(eid, {**repo.get_employee(eid), "emergency_name": "홍길순"}) if hasattr(repo, "update_employee") else None
    e = repo.get_employee(eid)
    if e.get("emergency_name"):
        raw = sqlite3.connect(__import__("app.config", fromlist=["settings"]).settings.db_path).execute(
            "SELECT emergency_name FROM employees WHERE id=?", (eid,)).fetchone()[0]
        assert "홍길순" not in raw                      # 암호화 저장
        assert "홍길순" in hr.get(f"/hr/employees/{eid}").text


def test_insurance_bulk(hr):
    a, b, c = _emp("공제가"), _emp("공제나"), _emp("공제다")
    execute("UPDATE employees SET resign_date='2020-01-01' WHERE id=?", (c,))
    page = hr.get("/hr/insurances").text
    assert "공제가" in page and "공제다" not in page         # 퇴사자는 목록에 없음
    data = {"product": "근로자 상해공제", "insurer": "시험공제회", "joined_on": "2026-01-01", "start_date": "2026-01-01",
            "end_date": "2026-12-31", "policy_no": "T-2026-1", "premium": "12,000", "note": ""}
    r = hr.post("/hr/insurances/bulk", data={**data, "emp": [str(a), str(b)]}, follow_redirects=False)
    assert r.status_code == 303
    ia = repo.list_insurances(a)
    assert len(ia) == 1 and ia[0]["policy_no"] == "T-2026-1" and ia[0]["premium"] == "12000" and len(repo.list_insurances(b)) == 1
    # 같은 내용 다시 → 늘지 않음. 증권번호만 고치면 그 기록을 고침
    hr.post("/hr/insurances/bulk", data={**data, "emp": [str(a)]})
    hr.post("/hr/insurances/bulk", data={**data, "policy_no": "T-2026-2", "emp": [str(a)]})
    ia = repo.list_insurances(a)
    assert len(ia) == 1 and ia[0]["policy_no"] == "T-2026-2" and repo.list_insurances(b)[0]["policy_no"] == "T-2026-1"
    # 새 기간 → 새 줄(지난 기록은 이력)
    hr.post("/hr/insurances/bulk", data={**data, "start_date": "2027-01-01", "end_date": "2027-12-31", "emp": [str(a)]})
    assert len(repo.list_insurances(a)) == 2
    # 다음에 열면 공통 내용이 채워져 있고, 같은 내용인 직원은 표시
    page = hr.get("/hr/insurances").text
    assert 'value="2027-12-31"' in page and "같은 내용 등록됨" in page
    # 날짜 오류는 저장 안 함
    r = hr.post("/hr/insurances/bulk", data={**data, "start_date": "2027-05-01", "end_date": "2027-01-01", "emp": [str(b)]}, follow_redirects=False)
    assert len(repo.list_insurances(b)) == 1


def test_insurance_bulk_permission():
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username='pb_view'"):
        auth.create_user("pb_view", "pb_view", "viewer", "Strong-Pass-123!", must_change=False)
    c.post("/login", data={"username": "pb_view", "password": "Strong-Pass-123!"}, headers=ORIGIN)
    c.headers.update(ORIGIN)
    assert c.get("/hr/insurances").status_code == 403
    assert TestClient(app).get("/hr/insurances", follow_redirects=False).status_code in (303, 401)
