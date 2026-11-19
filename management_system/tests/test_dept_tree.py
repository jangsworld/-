"""부서 대·중·소분류(3단계) 구분값."""
import re

import pytest
from fastapi.testclient import TestClient

from app.db import dept_path, dept_scope, dept_text, dept_tree, get_codes, query_one
from app.main import app
from app.modules.hr import repo as hr_repo
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
def admin():
    with TestClient(app):
        yield _client("dept_admin", "admin")


def _id(name):
    return query_one("SELECT id FROM codes WHERE group_code='DEPT' AND name=?", (name,))["id"]


def _add(admin, name, parent=None):
    admin.post("/settings/codes", data={"group_code": "DEPT", "name": name,
                                        "parent_id": str(_id(parent)) if parent else ""})
    return _id(name)


def test_three_levels_and_filters(admin):
    _add(admin, "T사업본부")
    _add(admin, "T기술그룹", "T사업본부")
    _add(admin, "T지원팀", "T기술그룹")
    rows = {r["name"]: r for r in dept_tree()}
    assert [rows[n]["level"] for n in ("T사업본부", "T기술그룹", "T지원팀")] == [1, 2, 3]
    assert dept_path("T지원팀") == "T사업본부 > T기술그룹 > T지원팀"
    assert dept_text("T지원팀").endswith("└ T지원팀") and dept_text("T사업본부") == "T사업본부"
    codes = get_codes("DEPT")
    assert codes.index("T사업본부") + 1 == codes.index("T기술그룹") and codes.index("T기술그룹") + 1 == codes.index("T지원팀")
    assert dept_scope("T사업본부") == {"T사업본부", "T기술그룹", "T지원팀"}
    assert dept_scope("T지원팀") == {"T지원팀"}

    # 4단계는 안 됨: 소분류 아래로 추가하면 대분류로 들어가고, 옮기기는 거부
    _add(admin, "T4단계", "T지원팀")
    assert {r["name"]: r for r in dept_tree()}["T4단계"]["level"] == 1
    r = admin.post(f"/settings/codes/{_id('T4단계')}/parent", data={"parent_id": str(_id("T지원팀"))}, follow_redirects=False)
    assert "3%EB%8B%A8%EA%B3%84" in r.headers["location"]          # '3단계'
    # 자기 하위 부서 아래로 옮기기 거부
    assert not hr_repo.set_dept_parent(_id("T사업본부"), _id("T지원팀"))
    # 하위가 2단계인 부서는 중분류 아래로 못 감
    _add(admin, "T다른본부")
    _add(admin, "T다른그룹", "T다른본부")
    assert not hr_repo.set_dept_parent(_id("T사업본부"), _id("T다른그룹"))
    assert hr_repo.set_dept_parent(_id("T기술그룹"), _id("T다른본부"))   # 그룹째 다른 본부로
    assert dept_path("T지원팀") == "T다른본부 > T기술그룹 > T지원팀"
    assert hr_repo.set_dept_parent(_id("T기술그룹"), _id("T사업본부"))

    # 하위가 있는 부서는 삭제 거부
    r = admin.post(f"/settings/codes/{_id('T기술그룹')}/delete", follow_redirects=False)
    assert query_one("SELECT 1 x FROM codes WHERE name='T기술그룹'")
    assert "%ED%95%98%EC%9C%84" in r.headers["location"]          # '하위'

    # 직원 조회: 본부를 고르면 소속 팀 직원까지
    from app.db import execute
    execute("INSERT INTO employees(name, emp_no, department, hire_date) VALUES ('부서시험', 'DT-1', 'T지원팀', '2026-01-01')")
    assert any(e["name"] == "부서시험" for e in hr_repo.list_employees(department="T사업본부"))
    assert not any(e["name"] == "부서시험" for e in hr_repo.list_employees(department="T다른본부"))
    page = admin.get("/hr/employees?department=T사업본부").text
    assert "부서시험" in page and 'value="T지원팀"' in page

    # 설정 화면
    page = admin.get("/settings/codes?group=DEPT").text
    assert "상위 부서" in page and "대분류" in page and "소분류" in page


def test_sibling_move_and_rename_keep_tree(admin):
    a = _add(admin, "M본부")
    _add(admin, "M1팀", "M본부")
    b = _add(admin, "M2팀", "M본부")
    admin.post(f"/settings/codes/{b}/move", data={"dir": "-1"})
    names = [r["name"] for r in dept_tree() if r["parent_id"] == a]
    assert names == ["M2팀", "M1팀"]
    admin.post(f"/settings/codes/{a}/rename", data={"name": "M사업본부"})
    assert dept_path("M1팀") == "M사업본부 > M1팀"
