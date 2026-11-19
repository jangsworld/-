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
