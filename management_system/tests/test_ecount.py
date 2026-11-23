"""이카운트 연동: 반영 대기열(신규·퇴사), 급여·연말정산 정보, 양식 읽기·엑셀 만들기, 입력 도우미 API."""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.db import query, query_one
from app.main import app
from app.modules.hr import ecount, repo
from app.security import auth

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='ec_hr'"):
            auth.create_user("ec_hr", "인사", "hr", "Strong-Pass-123!", must_change=False)
            auth.create_user("ec_view", "조회", "viewer", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "ec_hr", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


def _new_emp(name="이카운트신입", **kw):
    eid = repo.create_employee({"name": name, "hire_date": "2026-11-02", "department": "경영지원팀", "rank": "사원",
                                "employment_type": "정규직", "email": "ec@example.com", "bank_name": "국민",
                                "bank_account": "000-00-0001", **kw})
    return eid


def _xlsx_cells(data: bytes) -> list[list[str]]:
    return ecount._xlsx_rows(data)


def test_baseline_and_pending(client):
    # 기능 도입 전 직원은 '기존'으로 반영된 것으로 봄
    assert query_one("SELECT value FROM settings WHERE key='ecount_baseline_v1'")
    before = {(r["id"], r["action"]) for r in ecount.pending()}
    eid = _new_emp()
    rows = {(r["id"], r["action"]): r for r in ecount.pending()}
    assert (eid, "신규") in rows and (eid, "신규") not in before
    assert "주민번호 없음" in rows[(eid, "신규")]["warn"] and "보수월액 없음" in rows[(eid, "신규")]["warn"]
    page = client.get("/hr/ecount").text
    assert "이카운트신입" in page and "반영 대기" in page


def test_payroll_tab_and_values(client):
    eid = _new_emp("급여정보직원")
    repo.set_rrn(eid, "900101-1234567")
    r = client.post(f"/hr/employees/{eid}/payroll", data={
        "pay_기본급": "2,800,000", "pay_식대": "200000", "insured_monthly": "2800000", "householder": "Y",
        "dependents": "3", "children_8_20": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "저장" in r.headers["location"] or "%EC%A0%80%EC%9E%A5" in r.headers["location"]
    p = ecount.get_payroll(eid)
    assert p["pay_items"] == {"기본급": 2800000, "식대": 200000} and p["householder"] == "Y" and p["dependents"] == 3
    v = ecount.values(eid)
    assert v["rrn"] == "900101-1234567" and v["pay:기본급"] == "2800000" and v["account_holder"] == "급여정보직원"
    assert ecount.values(eid, include_rrn=False)["rrn"] == ""
    warn = next(r for r in ecount.pending() if r["id"] == eid)["warn"]
    assert warn == []
    # 잘못된 숫자·자녀>부양가족
    client.post(f"/hr/employees/{eid}/payroll", data={"dependents": "1", "children_8_20": "2"})
    assert ecount.get_payroll(eid)["dependents"] == 3
    page = client.get(f"/hr/employees/{eid}?tab=payroll").text
    assert "급여·연말정산 정보" in page and "2800000" in page and "반영 대기" in page


def test_template_read_and_export(client):
    eid = _new_emp("엑셀내보내기")
    repo.set_rrn(eid, "950505-2345678")
    ecount.save_payroll(eid, {"pay_items": {"기본급": 3000000}, "insured_monthly": 3000000})
    # 이카운트 양식처럼: 1줄 안내문, 2줄 머리글
    tpl = ecount.build_xlsx(["사원등록 엑셀 양식", "", ""], [["사원번호(필수)", "성명(필수)", "주민등록번호", "입사일자", "기본급", "알수없는칸"]])
    r = client.post("/hr/ecount/template", files={"file": ("사원등록양식.xlsx", tpl)}, follow_redirects=False)
    assert r.status_code == 303
    t = ecount.get_template()
    assert t["header_row"] == 2
    assert [c["field"] for c in t["columns"]] == ["emp_no", "name", "rrn", "hire_date", "pay:기본급", ""]
    client.post("/hr/ecount/settings", data={"pay_items": ecount.PAY_ITEMS_DEFAULT, "date_format": "YYYYMMDD"})
    r = client.post("/hr/ecount/export", data={"keys": [f"{eid}:신규"], "include_rrn": "1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxml")
    rows = _xlsx_cells(r.content)
    emp_no = query_one("SELECT emp_no FROM employees WHERE id=?", (eid,))["emp_no"]
    assert rows[0][:5] == ["사원번호(필수)", "성명(필수)", "주민등록번호", "입사일자", "기본급"]
    assert rows[1][:5] == [emp_no, "엑셀내보내기", "950505-2345678", "20261102", "3000000"]
    log = query_one("SELECT * FROM audit_logs WHERE action='개인정보 내보내기' ORDER BY id DESC LIMIT 1")
    assert "이카운트" in log["target"] and "주민번호 포함" in (log["detail"] or "")
    # 주민번호 빼고
    rows = _xlsx_cells(client.post("/hr/ecount/export", data={"keys": [f"{eid}:신규"]}).content)
    assert rows[1][2] == ""
    # 짝 수정 후 저장
    client.post("/hr/ecount/template/map", data={f"field_{i}": f for i, f in
                                                 enumerate(["emp_no", "name", "", "hire_date", "pay:기본급", "email"])})
    assert ecount.get_template()["columns"][5]["field"] == "email"
    # 잘못된 파일
    r = client.post("/hr/ecount/template", files={"file": ("x.txt", b"abc")}, follow_redirects=False)
    assert "xlsx" in r.headers["location"] or "%" in r.headers["location"]
    client.post("/hr/ecount/template/reset")
    assert ecount.get_template().get("default")
    client.post("/hr/ecount/settings", data={"pay_items": ecount.PAY_ITEMS_DEFAULT, "date_format": "YYYY-MM-DD"})


def test_mark_resign_and_undo(client):
    eid = _new_emp("반영표시직원")
    client.post("/hr/ecount/mark", data={"keys": [f"{eid}:신규"], "status": "완료", "method": "엑셀"})
    assert (eid, "신규") not in {(r["id"], r["action"]) for r in ecount.pending()}
    # 퇴사일을 넣으면 퇴사 대기로 올라옴
    repo.update_employee(eid, {**repo.query_one("SELECT * FROM employees WHERE id=?", (eid,)),
                               "resign_date": "2026-11-30", "resign_reason": "개인사정"})
    assert (eid, "퇴사") in {(r["id"], r["action"]) for r in ecount.pending()}
    p = ecount.helper_payload(eid, "퇴사")
    assert {f["key"] for f in p["fields"]} <= {"emp_no", "name", "resign_date", "resign_reason"}
    client.post("/hr/ecount/mark", data={"keys": [f"{eid}:퇴사"], "status": "제외"})
    hist = client.get("/hr/ecount?tab=history").text
    assert "반영표시직원" in hist
    sid = query_one("SELECT id FROM ecount_sync WHERE employee_id=? AND action='퇴사'", (eid,))["id"]
    client.post(f"/hr/ecount/history/{sid}/undo")
    assert (eid, "퇴사") in {(r["id"], r["action"]) for r in ecount.pending()}


def test_helper_api_and_zip(client):
    eid = _new_emp("도우미직원")
    repo.set_rrn(eid, "880808-1111111")
    data = client.get("/hr/ecount/api/pending").json()
    assert data["csrf"] and any(i["employee_id"] == eid for i in data["items"])
    p = client.post("/hr/ecount/api/payload", json={"employee_id": eid, "action": "신규"}).json()
    keys = {f["key"]: f for f in p["fields"]}
    assert keys["rrn"]["value"] == "880808-1111111" and "성명" in keys["name"]["aliases"]
    assert client.post("/hr/ecount/api/payload", json={"employee_id": eid, "action": "x"}).status_code == 400
    assert client.post("/hr/ecount/api/done", json={"employee_id": eid, "action": "신규"}).json()["marked"] == 1
    row = query_one("SELECT * FROM ecount_sync WHERE employee_id=? AND action='신규'", (eid,))
    assert row["method"] == "도우미" and row["done_by"] == "ec_hr"
    z = zipfile.ZipFile(io.BytesIO(client.get("/hr/ecount/helper.zip").content))
    names = z.namelist()
    assert "bms_ecount_helper/ecount_helper.py" in names and "bms_ecount_helper/run_helper.bat" in names
    assert "testserver" in z.read("bms_ecount_helper/config.json").decode()


def test_viewer_forbidden():
    with TestClient(app) as c:
        c.post("/login", data={"username": "ec_view", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        assert c.get("/hr/ecount", follow_redirects=False).status_code in (303, 403)
        assert c.get("/hr/ecount/api/pending", follow_redirects=False).status_code in (303, 403)
        assert c.get("/hr/ecount/helper.zip", follow_redirects=False).status_code in (303, 403)


def test_guess_field():
    assert ecount.guess_field("사원코드") == "emp_no"
    assert ecount.guess_field("입사일자(YYYYMMDD)") == "hire_date"
    assert ecount.guess_field("건강보험 보수월액") == "insured_monthly"
    assert ecount.guess_field("식대") == "pay:식대"
    assert ecount.guess_field("비고") == ""


def test_robot_jobs_key_and_flow(client):
    """로봇: 열쇠 없이는 못 쓰고, 요청한 직원 1명 값만 받아 가고, 대조 결과에 따라 완료·실패."""
    from app.db import execute, set_setting
    from app.modules.hr import ecount_robot as er
    eid = _new_emp("로봇시험직원", birth_date="1990-01-01")
    repo.set_rrn(eid, "900101-1000000")
    # 열쇠가 없으면 요청 불가, 통로도 막힘
    set_setting("ecount_robot_key", "")
    r = client.post("/hr/ecount/robot/request", data={"keys": f"{eid}:신규"}, follow_redirects=False)
    assert r.status_code == 303 and "tab=robot" in r.headers["location"]
    assert client.post("/robot/ecount/claim", json={}).status_code == 403
    # 열쇠는 관리자만
    r = client.post("/hr/ecount/robot/key", follow_redirects=False)
    assert r.status_code == 303 and not er.status()["has_key"]
    key = er.new_key()
    assert er.check_key(key) and not er.check_key(key + "x") and not er.check_key("")
    assert key not in (query_one("SELECT value FROM settings WHERE key='ecount_robot_key'")["value"])   # 지문만 저장
    H = {"x-robot-key": key}
    assert client.post("/robot/ecount/claim", json={}, headers={"x-robot-key": "wrong"}).status_code == 403
    assert client.post("/robot/ecount/claim", json={}, headers=H).json() == {"ok": True, "job": None}     # 요청이 없으면 아무것도 안 줌
    assert er.status()["online"]
    # 부서 목록·짝 (하위 부서는 상위 부서의 짝을 따름)
    assert client.post("/robot/ecount/depts", json={"depts": [["00002", "경영관리그룹"], ["00038", "사업수행그룹"]]}, headers=H).json()["saved"] == 2
    client.post("/hr/ecount/robot/settings", data={"dept:경영지원팀": "00002", "dept:없는부서": "99999", "pay_std": "00001", "ok_confirms": "사업자단위과세"})
    assert er.dept_map() == {"경영지원팀": "00002"} and er.dept_code_for("경영지원팀") == "00002" and er.dept_code_for("모르는팀") == ""
    # 요청 → 가져가기 → 값
    r = client.post("/hr/ecount/robot/request", data={"keys": f"{eid}:신규"}, follow_redirects=False)
    assert r.status_code == 303
    assert er.request([(eid, "신규")], "x") == 0                                    # 같은 직원 두 번 요청 안 됨
    page = client.get("/hr/ecount").text
    assert "요청함" in page and "로봇으로 이카운트에 넣기" in page
    job = client.post("/robot/ecount/claim", json={}, headers=H).json()["job"]
    v = job["values"]
    assert job["action"] == "신규" and v["name"] == "로봇시험직원" and v["rrn"] == "900101-1000000" and v["dept_code"] == "00002"
    assert v["pay_std_code"] == "00001" and job["ok_confirms"] == ["사업자단위과세"]
    assert client.post("/robot/ecount/claim", json={}, headers=H).json()["job"] is None       # 한 번만 내줌
    assert "넣는 중" in client.get("/hr/ecount").text
    # 저장됐지만 값이 다르면 실패 → 대기 그대로
    jid = job["job_id"]
    assert client.post("/robot/ecount/finish", json={"job_id": jid, "ok": True, "message": "", "detail": {"saved": True, "mismatch": ["계좌번호"]}}, headers=H).json()["ok"]
    assert (eid, "신규") in {(x["id"], x["action"]) for x in ecount.pending()}
    assert "다른 칸: 계좌번호" in client.get("/hr/ecount").text
    assert not client.post("/robot/ecount/finish", json={"job_id": jid, "ok": True, "detail": {"saved": True}}, headers=H).json()["ok"]   # 끝난 일감은 다시 못 바꿈
    # 다시 요청 → 완료 → 반영 기록
    assert er.request([(eid, "신규")], "x") == 1
    job = er.claim()
    assert er.finish(job["job_id"], True, "", {"saved": True, "skipped": [["부서", "짝 없음"]], "mismatch": []})
    assert (eid, "신규") not in {(x["id"], x["action"]) for x in ecount.pending()}
    row = query_one("SELECT * FROM ecount_sync WHERE employee_id=? AND action='신규'", (eid,))
    assert row["method"] == "로봇" and "못 넣은 칸: 부서" in row["note"]
    # 처리중인 채 오래되면 실패로
    eid2 = _new_emp("로봇시간초과")
    er.request([(eid2, "신규")], "x")
    j2 = er.claim()
    execute("UPDATE ecount_robot_jobs SET claimed_at='2020-01-01 00:00:00' WHERE id=?", (j2["job_id"],))
    assert er.latest_jobs()[(eid2, "신규")]["status"] == "실패"
    # 요청 취소
    er.request([(eid2, "신규")], "x")
    jid3 = er.latest_jobs()[(eid2, "신규")]["id"]
    assert client.post(f"/hr/ecount/robot/cancel/{jid3}", follow_redirects=False).status_code == 303
    assert er.latest_jobs()[(eid2, "신규")]["status"] == "취소"
    # 로봇 탭 화면
    page = client.get("/hr/ecount?tab=robot").text
    assert "부서 짝 맞추기" in page and "경영관리그룹 (00002)" in page and key not in page
