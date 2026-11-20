"""퇴사 절차 안내: 안내문 발송·진행표·권한."""
import re

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.db import execute, get_codes, query, query_one
from app.main import app
from app.modules.hr import offboard
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
        yield _client("ob_hr", "hr")


def _emp(name, email="leaver@example.com"):
    cols = {"name": name, "emp_no": "OB-" + name, "department": get_codes("DEPT")[0], "rank": get_codes("RANK")[0],
            "hire_date": "2019-03-04", "email": email}
    execute(f"INSERT INTO employees({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
    return query_one("SELECT id FROM employees WHERE name=?", (name,))["id"]


def test_offboard_flow(hr, monkeypatch):
    eid = _emp("퇴사안내시험")
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, *a, **k: sent.append((to, subject, body)))

    # 인사기록카드 버튼, 첫 화면(단계 안내·기본 글귀)
    assert f'href="/hr/offboard/{eid}"' in hr.get(f"/hr/employees/{eid}").text
    r = hr.get("/hr/offboard")
    assert r.status_code == 200 and "퇴사 처리 단계" in r.text and "{퇴직일}" in r.text and "퇴사안내시험" in r.text
    assert hr.get(f"/hr/offboard/start?emp={eid}", follow_redirects=False).headers["location"] == f"/hr/offboard/{eid}"

    # 퇴직 예정일을 저장하기 전에는 보내기·체크 불가
    r = hr.get(f"/hr/offboard/{eid}")
    assert "퇴직 예정일</b>을 저장하세요" in r.text and "(퇴직일 미정)" in r.text
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "employee", "subject": "s", "body": "b"})
    assert not sent
    assert hr.post(f"/hr/offboard/{eid}/check", json={"key": "a1", "done": True}).status_code == 400

    # 저장: 기한이 퇴직일보다 늦으면 거절
    r = hr.post(f"/hr/offboard/{eid}/save", data={"last_day": "2027-01-29", "handover_due": "2027-02-05"}, follow_redirects=False)
    assert offboard.get(eid) is None
    hr.post(f"/hr/offboard/{eid}/save", data={"last_day": "2027-01-29", "handover_due": "2027-01-26", "successor": "홍길동 과장"})
    ob = offboard.get(eid)
    assert ob["last_day"] == "2027-01-29" and ob["successor"] == "홍길동 과장"
    assert query_one("SELECT resign_date FROM employees WHERE id=?", (eid,))["resign_date"] in (None, "")   # 인사기록카드는 그대로
    page = hr.get(f"/hr/offboard/{eid}").text
    assert "2027년 1월 29일" in page and "2027년 1월 26일" in page and "홍길동 과장" in page and "le***@example.com" in page
    assert "leaver@example.com" not in page and "2027-02-12" in page

    # 체크
    r = hr.post(f"/hr/offboard/{eid}/check", json={"key": "a1", "done": True})
    assert r.json()["done"] == 1
    assert hr.post(f"/hr/offboard/{eid}/check", json={"key": "zz", "done": True}).status_code == 400
    assert hr.post(f"/hr/offboard/{eid}/check", json={"key": "a1", "done": False}).json()["done"] == 0

    # 안내문 발송: 채워지지 않은 항목이 있으면 거절, 정상 발송은 기록 + b1 자동 체크
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "employee", "subject": "안내", "body": "{이름}님"})
    assert not sent
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "manual", "manual_to": "나쁜주소", "subject": "안내", "body": "본문"})
    assert not sent
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "employee", "subject": "퇴직 절차 안내", "body": "본문 <b>굵게</b>"})
    assert sent == [("leaver@example.com", "퇴직 절차 안내", "본문 <b>굵게</b>")]
    raw = query("SELECT how FROM offboard_mails WHERE employee_id=?", (eid,))
    assert [m["how"] for m in raw] == ["메일(인사기록카드)"]
    import sqlite3
    from app.config import settings
    stored = sqlite3.connect(settings.db_path).execute("SELECT to_addr, body FROM offboard_mails WHERE employee_id=?", (eid,)).fetchone()
    assert stored[0].startswith("enc:v1:") and stored[1].startswith("enc:v1:")          # 주소·본문은 암호화 저장
    assert "b1" in offboard.checks(eid)
    page = hr.get(f"/hr/offboard/{eid}").text
    assert "본문 &lt;b&gt;굵게&lt;/b&gt;" in page and "본문 <b>굵게</b>" not in page       # 기록 표시 시 글자 그대로
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "hand", "subject": "안내", "body": "직접 전달"})
    assert len(sent) == 1 and len(offboard.mails(eid)) == 2
    assert not query("SELECT 1 FROM audit_logs WHERE detail LIKE '%leaver@example.com%'")

    # 목록, 완료·다시 열기, 삭제
    assert "퇴사안내시험" in hr.get("/hr/offboard").text
    hr.post(f"/hr/offboard/{eid}/close")
    assert offboard.get(eid)["closed_at"]
    hr.post(f"/hr/offboard/{eid}/close", data={"reopen": "1"})
    assert not offboard.get(eid)["closed_at"]
    hr.post(f"/hr/offboard/{eid}/delete")
    assert offboard.get(eid) is None and not offboard.mails(eid) and not offboard.checks(eid)
    assert query_one("SELECT 1 AS x FROM employees WHERE id=?", (eid,))


def test_offboard_template_and_roles(hr):
    hr.post("/hr/offboard/template", data={"subject": "[{회사명}] 안내 {이름}", "body": "{이름}님 {퇴직일}까지"})
    t = offboard.templates_text()
    assert t["custom"] and t["body"] == "{이름}님 {퇴직일}까지"
    eid = _emp("퇴사글귀시험", email="")
    page = hr.get(f"/hr/offboard/{eid}").text
    assert "퇴사글귀시험님 (퇴직일 미정)까지" in page and "등록된 주소 없음" in page
    hr.post("/hr/offboard/template", data={"reset": "1", "subject": "x", "body": "y"})
    assert not offboard.templates_text()["custom"] and offboard.templates_text()["body"] == offboard.DEFAULT_BODY
    # 조회자·총무 담당은 접근 불가
    with TestClient(app):
        for user, role in (("ob_view", "viewer"), ("ob_ga", "ga")):
            c = _client(user, role)
            assert c.get("/hr/offboard", follow_redirects=False).status_code == 403
            assert c.get(f"/hr/offboard/{eid}", follow_redirects=False).status_code == 403
            assert c.post(f"/hr/offboard/{eid}/send", data={"target": "hand", "subject": "a", "body": "b"}, follow_redirects=False).status_code == 403
            assert "/hr/offboard" not in c.get(f"/hr/employees/{eid}").text
