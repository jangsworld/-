"""개인별 연봉 현황·과거 연봉 올리기, 인사발령 메뉴."""
import io
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.common import today
from app.db import execute, get_codes, query_one
from app.main import app
from app.modules.hr import appoint, repo
from app.modules.hr import salary_person as sp
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
        yield _client("sa_hr", "hr")


def _emp(name, hire="2018-08-01"):
    cols = {"name": name, "emp_no": "SA-" + name, "department": get_codes("DEPT")[0], "rank": get_codes("RANK")[0], "hire_date": hire}
    execute(f"INSERT INTO employees({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
    return query_one("SELECT id FROM employees WHERE name=?", (name,))["id"]


def _upload(hr, text, **data):
    return hr.post("/hr/salaries/history/import", files={"file": ("h.csv", io.BytesIO(text.encode("utf-8-sig")), "text/csv")},
                   data=data, follow_redirects=False)


def _sal(emp_id):
    return {s["year"]: s["annual_salary"] for s in repo.employee_salaries(emp_id)}


def test_person_salary_page_and_inline_save(hr):
    eid = _emp("연봉가")
    y = today().year
    r = hr.post(f"/hr/salaries/person/{eid}", data={"y_2018": "32,000,000", "y_2019": "32000000", "y_2020": "37,800,000",
                                                    f"y_{y}": "45,855,600"}, follow_redirects=False)
    assert r.status_code == 303
    v = sp.view(eid)
    by = {x["year"]: x for x in v["rows"]}
    assert v["rows"][0]["year"] == 2018 and v["rows"][-1]["year"] == y
    assert by[2019]["diff"] == 0 and by[2020]["diff"] == 5_800_000 and round(by[2020]["rate"], 1) == 18.1
    assert by[2021]["amount"] is None and by[y]["diff"] == 45_855_600 - 37_800_000      # 빈 해는 건너뛰고 직전 값과 비교
    assert v["summary"]["total"] == 13_855_600 and v["summary"]["cagr"] is not None
    page = hr.get(f"/hr/salaries/person/{eid}").text
    assert 'name="y_2018"' in page and "연평균 인상률" in page and "cPerson" in page
    # 칸을 비우면 그 해 삭제, 잘못된 금액은 전체 거부
    hr.post(f"/hr/salaries/person/{eid}", data={"y_2019": ""})
    assert 2019 not in _sal(eid)
    hr.post(f"/hr/salaries/person/{eid}", data={"y_2021": "abc", "y_2022": "40000000"})
    assert 2022 not in _sal(eid)
    assert f'/hr/salaries/person/{eid}"' in hr.get("/hr/salaries?tab=people").text
    viewer = _client("sa_view", "viewer")
    assert viewer.get(f"/hr/salaries/person/{eid}").status_code == 403


def test_history_csv_import(hr):
    a, b = _emp("올리기가", "2019-03-01"), _emp("올리기나", "2020-01-02")
    repo.upsert_salary(a, 2020, 30_000_000)
    tpl = hr.get("/hr/salaries/history/template.csv").text
    assert "사번" in tpl.splitlines()[0] and "2019" in tpl.splitlines()[0] and "올리기가" in tpl
    text = '사번,성명,2019,2020,2021\nSA-올리기가,올리기가,"28,000,000",31000000,33000000\n,올리기나,,25000000,\n'
    assert _upload(hr, text).status_code == 303
    assert _sal(a) == {2019: 28_000_000, 2020: 30_000_000, 2021: 33_000_000}            # 이미 있던 2020 은 그대로
    assert _sal(b) == {2020: 25_000_000}
    _upload(hr, text, overwrite="1")
    assert _sal(a)[2020] == 31_000_000
    # 한 줄이라도 틀리면 아무것도 저장하지 않음
    _upload(hr, "사번,성명,2022\nSA-올리기가,올리기가,35000000\nX-없음,없는사람,1000\n")
    assert 2022 not in _sal(a)
    _upload(hr, "성명,2022\n올리기가,삼천만원\n")
    assert 2022 not in _sal(a)


def test_appointments_apply_bulk_planned_history(hr):
    depts, ranks, locs = get_codes("DEPT"), get_codes("RANK"), get_codes("LOCATION")
    a, b = _emp("발령가"), _emp("발령나")
    t = today()
    base = {"appt_date": t.isoformat(), "batch_title": "시험 조직개편", "mode": "apply"}
    # 일괄 적용: 두 명을 다른 부서로 + 근무지
    r = hr.post("/hr/appointments/register", data={**base, "emp": [str(a), str(b)], "to_department": depts[1], "to_work_location": locs[0]},
                follow_redirects=False)
    assert r.status_code == 303
    ea = repo.get_employee(a)
    assert ea["department"] == depts[1] and ea["work_location"] == locs[0]
    rows = appoint.rows(batch_title="시험 조직개편")
    assert len(rows) == 4 and {r["appt_type"] for r in rows} == {"전보", "근무지변경"}
    assert all(r["from_value"] == depts[0] for r in rows if r["appt_type"] == "전보")
    mv = {m["department"]: m for m in appoint.movement(rows)}
    assert mv[depts[1]]["net"] == 2 and mv[depts[0]]["net"] == -2
    page = hr.get("/hr/appointments?batch=시험 조직개편").text
    assert "부서 이동 현황" in page and "발령가" in page and "발령 통지 문서" in page
    # 같은 값으로는 다시 기록하지 않음
    hr.post("/hr/appointments/register", data={**base, "emp": [str(a)], "to_department": depts[1]})
    assert len(appoint.rows(batch_title="시험 조직개편")) == 4
    # 예정: 정보는 그대로, 발령일이 되면 적용
    fut = (t + timedelta(days=10)).isoformat()
    hr.post("/hr/appointments/register", data={"appt_date": fut, "mode": "apply", "emp": [str(a)], "to_rank": ranks[1], "batch_title": "예정 승진"})
    assert repo.get_employee(a)["rank"] == ranks[0]
    row = appoint.rows(batch_title="예정 승진")[0]
    assert row["planned"] and appoint.apply_due() == 0
    execute("UPDATE appointments SET appt_date=? WHERE id=?", (t.isoformat(), row["id"]))
    assert appoint.apply_due() == 1 and repo.get_employee(a)["rank"] == ranks[1]
    row = appoint.rows(batch_title="예정 승진")[0]
    assert not row["planned"] and row["from_value"] == ranks[0]
    # 이력만(소급): 지금 정보는 그대로
    hr.post("/hr/appointments/register", data={"appt_date": "2021-03-01", "mode": "history", "emp": [str(b)], "to_rank": ranks[2],
                                               "from_rank": ranks[1], "batch_title": "2021 소급"})
    assert repo.get_employee(b)["rank"] == ranks[0]
    h = appoint.rows(batch_title="2021 소급")[0]
    assert h["status"] == "이력" and h["from_value"] == ranks[1] and h["to_value"] == ranks[2]
    # 검증: 목록에 없는 값·직원 없음·항목 없음·미래의 이력만
    n = len(appoint.rows())
    for bad in ({"emp": [str(a)], "to_department": "없는부서"}, {"emp": [], "to_rank": ranks[0]}, {"emp": [str(a)]},
                {"emp": [str(a)], "to_rank": ranks[0], "mode": "history", "appt_date": fut}):
        hr.post("/hr/appointments/register", data={**base, **bad})
    assert len(appoint.rows()) == n
    # 인사기록카드에서 근무지를 바꿔도 기록됨
    repo.update_employee(b, {**repo.get_employee(b), "work_location": locs[1]})
    assert any(r["appt_type"] == "근무지변경" and r["to_value"] == locs[1] and r["employee_id"] == b for r in appoint.rows())
    # 내려받기·권한·삭제
    assert "시험 조직개편" in hr.get("/hr/appointments/export.csv").text
    viewer = _client("sa_view2", "viewer")
    assert viewer.get("/hr/appointments").status_code == 403
    assert viewer.post("/hr/appointments/register", data={**base, "emp": [str(a)], "to_rank": ranks[0]}).status_code == 403
    hr.post(f"/hr/appointments/{h['id']}/remove")
    assert not appoint.rows(batch_title="2021 소급")


def test_appointment_notice_html_escapes():
    html_ = appoint.notice_html([{"name": "<b>가</b>", "appt_type": "전보", "from_value": "A", "to_value": "B",
                                  "appt_date": "2026-01-02"}], "제목<script>")
    assert "<script>" not in html_ and "&lt;b&gt;가" in html_ and "인 사 발 령" in html_
