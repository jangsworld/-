"""전체 화면 렌더링 및 주요 업무 흐름 스모크 테스트."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MGMT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.common import korean_amount  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


PAGES = [
    "/hr/dashboard", "/hr/employees", "/hr/employees?status=", "/hr/employees/new", "/hr/employees/1",
    "/hr/employees/1?tab=history", "/hr/employees/1?tab=contract", "/hr/employees/1?tab=salary",
    "/hr/employees/1?tab=reward", "/hr/employees/1?tab=special", "/hr/employees/1/edit",
    "/hr/contracts", "/hr/contracts/new", "/hr/contracts/new?employee_id=2", "/hr/documents/1",
    "/hr/templates", "/hr/templates/1", "/hr/templates/0", "/hr/certificates",
    "/hr/salaries", "/hr/salaries?year=2025", "/hr/rewards", "/hr/specials", "/settings",
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


def test_employee_lifecycle(client):
    r = client.post("/hr/employees/new", data={
        "name": "테스트", "hire_date": "2026-03-02", "department": "인사팀", "rank": "사원",
        "employment_type": "계약직(기간제)", "initial_salary": "36000000",
    }, follow_redirects=False)
    assert r.status_code == 303
    emp_id = int(r.headers["location"].split("/")[3].split("?")[0])

    # 직급 변경 → 발령 이력 자동 기록
    client.post(f"/hr/employees/{emp_id}/edit", data={
        "name": "테스트", "hire_date": "2026-03-02", "department": "인사팀", "rank": "주임",
        "employment_type": "계약직(기간제)", "appt_date": "2026-10-01",
    })
    page = client.get(f"/hr/employees/{emp_id}?tab=history").text
    assert "승진" in page and "주임" in page

    # 계약서 자동작성 → 치환 확인 → 체결
    tpl_id = next(t for t in __import__("app.modules.hr.repo", fromlist=["x"]).list_templates("contract")
                  if t["category"] == "기간제")["id"]
    r = client.post("/hr/contracts/new", data={
        "employee_id": emp_id, "template_id": tpl_id, "start_date": "2026-03-02", "end_date": "2027-03-01",
        "annual_salary": "36000000", "weekly_hours": "40",
    }, follow_redirects=False)
    doc_id = int(r.headers["location"].split("/")[3].split("?")[0])
    page = client.get(f"/hr/documents/{doc_id}").text
    assert "테스트" in page and "삼천육백만" in page and "2027년 03월 01일" in page
    assert client.post(f"/hr/api/documents/{doc_id}", json={
        "content": "<p>수정<script>alert(1)</script></p>", "status": "체결완료"}).json()["ok"]
    page = client.get(f"/hr/documents/{doc_id}").text
    assert "<script>alert(1)" not in page

    # 연봉 일괄 인상 미리보기
    prev = client.get("/hr/api/salaries/bulk-preview", params={"year": 2027, "rate": 5, "round_unit": 10000}).json()
    row = next(x for x in prev["rows"] if x["employee_id"] == emp_id)
    assert row["new"] == 37800000

    # 상벌 / 근로특이사항
    client.post("/hr/rewards/save", data={"employee_id": emp_id, "kind": "징계", "category": "견책",
                                          "action_date": "2026-05-01", "reason": "테스트"})
    assert "2027-05-01" in client.get(f"/hr/employees/{emp_id}?tab=reward").text  # 말소일 자동 계산
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
