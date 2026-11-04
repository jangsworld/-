"""전체 화면 렌더링 및 주요 업무 흐름 스모크 테스트 (인사담당 권한)."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.common import korean_amount

ORIGIN = {"origin": "http://testserver"}


def login(c: TestClient, username: str, password: str):
    r = c.post("/login", data={"username": username, "password": password, "next": "/"}, headers=ORIGIN,
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r


def csrf(c: TestClient) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get("/hr/dashboard").text).group(1)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        from app.security import auth
        if not c.app.state.__dict__.get("hr_ready"):
            try:
                auth.create_user("hruser", "인사담당", "hr", "Hr-User-Pass1!", must_change=False)
            except Exception:
                pass
            c.app.state.hr_ready = True
        login(c, "hruser", "Hr-User-Pass1!")
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = csrf(c)
        yield c


PAGES = [
    "/hr/dashboard", "/hr/employees", "/hr/employees?status=", "/hr/employees/new", "/hr/employees/1",
    "/hr/employees/1?tab=history", "/hr/employees/1?tab=contract", "/hr/employees/1?tab=salary",
    "/hr/employees/1?tab=reward", "/hr/employees/1?tab=special", "/hr/employees/1/edit",
    "/hr/contracts", "/hr/contracts/new", "/hr/contracts/new?employee_id=2", "/hr/documents/1",
    "/hr/templates", "/hr/templates/1", "/hr/templates/0", "/hr/certificates",
    "/hr/salaries", "/hr/salaries?year=2025", "/hr/rewards", "/hr/specials", "/account",
    "/hr/employees/export.csv",
]


@pytest.mark.parametrize("url", PAGES)
def test_pages(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text[:500]


def test_korean_amount():
    assert korean_amount(36000000) == "삼천육백만"
    assert korean_amount(102500000) == "일억이백오십만"
    assert korean_amount(10000) == "일만"


def _id_from(r):
    return int(r.headers["location"].split("/")[3].split("?")[0])


def test_employee_lifecycle(client):
    r = client.post("/hr/employees/new", data={
        "name": "테스트", "hire_date": "2026-03-02", "department": "인사팀", "rank": "사원", "phone": "010-1111-2222",
        "employment_type": "계약직(기간제)", "initial_salary": "36000000",
    }, follow_redirects=False)
    assert r.status_code == 303
    emp_id = _id_from(r)

    client.post(f"/hr/employees/{emp_id}/edit", data={
        "name": "테스트", "hire_date": "2026-03-02", "department": "인사팀", "rank": "주임", "phone": "010-1111-2222",
        "employment_type": "계약직(기간제)", "appt_date": "2026-10-01",
    })
    page = client.get(f"/hr/employees/{emp_id}?tab=history").text
    assert "승진" in page and "주임" in page
    assert "테스트" in client.get("/hr/employees", params={"q": "1111-2222"}).text  # 암호화 필드 검색

    from app.modules.hr import repo
    tpl_id = next(t for t in repo.list_templates("contract") if t["category"] == "기간제")["id"]
    r = client.post("/hr/contracts/new", data={
        "employee_id": emp_id, "template_id": tpl_id, "start_date": "2026-03-02", "end_date": "2027-03-01",
        "annual_salary": "36000000", "weekly_hours": "40",
    }, follow_redirects=False)
    doc_id = _id_from(r)
    page = client.get(f"/hr/documents/{doc_id}").text
    assert "테스트" in page and "삼천육백만" in page and "2027년 03월 01일" in page
    assert client.post(f"/hr/api/documents/{doc_id}", json={
        "content": "<p>수정<script>alert(1)</script><img src=x onerror=alert(1)></p>", "status": "작성중"}).json()["ok"]
    page = client.get(f"/hr/documents/{doc_id}").text
    assert "alert(1)" not in page

    prev = client.get("/hr/api/salaries/bulk-preview", params={"year": 2027, "rate": 5, "round_unit": 10000}).json()
    row = next(x for x in prev["rows"] if x["employee_id"] == emp_id)
    assert row["new"] == 37800000

    client.post("/hr/rewards/save", data={"employee_id": emp_id, "kind": "징계", "category": "견책",
                                          "action_date": "2026-05-01", "reason": "테스트"})
    assert "2027-05-01" in client.get(f"/hr/employees/{emp_id}?tab=reward").text
    client.post("/hr/specials/save", data={"employee_id": emp_id, "category": "육아기 근로시간 단축",
                                           "start_date": "2026-01-01", "end_date": "2099-12-31", "reduced_hours": "30"})
    assert "단축근무" in client.get(f"/hr/employees/{emp_id}").text


def test_certificate(client):
    from app.modules.hr import repo
    tpl = repo.list_templates("certificate")[0]
    r = client.post("/hr/certificates/new", data={"employee_id": 1, "template_id": tpl["id"], "purpose": "은행제출"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "은행제출" in client.get(r.headers["location"]).text


def test_template_value_escaped(client):
    """임직원 이름 등에 태그를 넣어도 계약서에 스크립트로 삽입되지 않아야 함."""
    r = client.post("/hr/employees/new", data={"name": "<b>악성</b>", "hire_date": "2026-01-01",
                                               "employment_type": "정규직"}, follow_redirects=False)
    emp_id = _id_from(r)
    from app.modules.hr import repo
    tpl_id = next(t for t in repo.list_templates("contract") if t["category"] == "정규직")["id"]
    r = client.post("/hr/contracts/new", data={"employee_id": emp_id, "template_id": tpl_id,
                                               "start_date": "2026-01-01"}, follow_redirects=False)
    doc = repo.get_document(_id_from(r))
    assert "&lt;b&gt;악성&lt;/b&gt;" in doc["content"]
