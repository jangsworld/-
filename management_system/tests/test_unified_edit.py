"""인사기록 한 화면 수정: 기본정보 + 연봉 + 급여·연말정산 + 상해공제 + 체크리스트를 저장 한 번으로. 틀리면 아무것도 안 바뀜."""
import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_codes
from app.main import app
from app.modules.hr import ecount, repo
from app.security import auth

O = {"origin": "http://testserver"}


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        if not auth.query_one("SELECT 1 FROM users WHERE username='ue_hr'"):
            auth.create_user("ue_hr", "ue_hr", "hr", "Ue-Strong-Pass-1!", must_change=False)
        client.post("/login", data={"username": "ue_hr", "password": "Ue-Strong-Pass-1!"}, headers=O)
        client.headers.update(O)
        client.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/account").text).group(1)
        yield client


def _base(eid):
    e = repo.get_employee(eid)
    return {"name": e["name"], "hire_date": e["hire_date"], "employment_type": e["employment_type"] or "정규직",
            "department": e.get("department") or "", "emp_no": e.get("emp_no") or ""}


def test_one_save_everything(c):
    eid = repo.create_employee({"name": "한번저장", "hire_date": "2022-04-01", "birth_date": "1991-01-01", "department": "영업팀"})
    page = c.get(f"/hr/employees/{eid}/edit")
    assert page.status_code == 200
    for sec in ("s-salary", "s-payroll", "s-ins", "s-check"):
        assert f'id="{sec}"' in page.text
    items = ecount.pay_items()
    onboard = get_codes("ONBOARD")
    data = {**_base(eid), "job_duty": "영업 관리", "sal_year": "2026", "sal_amount": "74614164", "sal_date": "2026-01-01",
            "sal_reason": get_codes("SALARY_REASON")[0], "insured_monthly": "5,000,000", "householder": "Y", "dependents": "2",
            "children_8_20": "1", f"pay_{items[0]}": "4000000", "ins_product": "근로자 상해공제", "ins_insurer": "공제회",
            "ins_start_date": "2026-01-01", "ins_end_date": "2026-12-31", "ins_premium": "120,000",
            "checks_shown": "1", "chk_ONBOARD_shown": "1", "chk_ONBOARD_0": "1", "back_tab": "salary"}
    r = c.post(f"/hr/employees/{eid}/edit", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/hr/employees/{eid}?tab=salary"), r.headers["location"]
    assert repo.get_employee(eid)["job_duty"] == "영업 관리"
    sal = {s["year"]: s for s in repo.employee_salaries(eid)}
    assert sal[2026]["annual_salary"] == 74_614_164                    # 1만 원 단위가 아닌 금액도 그대로
    p = ecount.get_payroll(eid)
    assert p["insured_monthly"] == 5_000_000 and p["dependents"] == 2 and p["pay_items"][items[0]] == 4_000_000
    ins = repo.list_insurances(eid)
    assert len(ins) == 1 and ins[0]["product"] == "근로자 상해공제" and str(ins[0]["premium"]) == "120000"
    assert ("ONBOARD", onboard[0]) in repo.list_checks(eid)
    # 같은 값으로 다시 저장해도 중복 기록 없음, 체크 해제 반영
    data2 = {**data, "ins_id": str(ins[0]["id"])}
    data2.pop("chk_ONBOARD_0")
    c.post(f"/hr/employees/{eid}/edit", data=data2)
    assert len(repo.list_insurances(eid)) == 1
    assert ("ONBOARD", onboard[0]) not in repo.list_checks(eid)


def test_wrong_value_changes_nothing(c):
    eid = repo.create_employee({"name": "원자저장", "hire_date": "2022-04-01", "birth_date": "1991-01-01"})
    data = {**_base(eid), "job_duty": "바뀌면 안 됨", "sal_year": "2026", "sal_amount": "74614164",
            "insured_monthly": "abc"}                                    # 급여 칸이 틀림
    r = c.post(f"/hr/employees/{eid}/edit", data=data, follow_redirects=False)
    assert r.status_code == 303 and "/edit" in r.headers["location"]
    assert repo.get_employee(eid)["job_duty"] != "바뀌면 안 됨"
    assert not [s for s in repo.employee_salaries(eid) if s["year"] == 2026]


def test_salary_modal_accepts_any_won(c):
    eid = repo.create_employee({"name": "연봉원단위", "hire_date": "2022-04-01", "birth_date": "1991-01-01"})
    r = c.post("/hr/salaries", data={"employee_id": str(eid), "year": "2026", "annual_salary": "74614164",
                                     "effective_date": "2026-01-01", "reason": "신규", "next": f"/hr/employees/{eid}?tab=salary"},
               follow_redirects=False)
    assert r.status_code == 303
    assert {s["year"]: s for s in repo.employee_salaries(eid)}[2026]["annual_salary"] == 74_614_164
    page = c.get(f"/hr/employees/{eid}?tab=salary").text
    m = re.search(r'name="annual_salary"[^>]*', page)
    assert m and 'step="10000"' not in m.group(0)
    # 칸이 비면 서버 오류 대신 안내
    r = c.post("/hr/salaries", data={"employee_id": "", "year": "2026", "annual_salary": ""}, follow_redirects=False,
               headers={"referer": f"http://testserver/hr/employees/{eid}?tab=salary"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
