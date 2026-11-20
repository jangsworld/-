"""퇴사 절차 안내: 안내문 발송·진행표·권한."""
import re

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.db import execute, get_codes, query, query_one, set_setting
from app.main import app
from app.modules.hr import offboard, richtext
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
    htmls = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, *a, **k: (sent.append((to, subject, body)), htmls.append(k.get("html"))))

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
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "employee", "subject": "퇴직 절차 안내", "body": "<div>   </div><br>"})
    assert not sent                                                                     # 글자가 없는 글은 거절
    bad = '본문 <b>굵게</b><script>xss_mark_1()</script><img src=x onerror="xss_mark_2()"><span style="color: red; position: fixed" onclick="xss_mark_3()">색</span>'
    hr.post(f"/hr/offboard/{eid}/send", data={"target": "employee", "subject": "퇴직 절차 안내", "body": bad})
    assert sent == [("leaver@example.com", "퇴직 절차 안내", "본문 굵게색")]                 # 글자만 있는 본문
    assert '본문 <b>굵게</b><span style="color: red">색</span>' in htmls[0] and "xss_mark" not in htmls[0] and "position" not in htmls[0]
    raw = query("SELECT how FROM offboard_mails WHERE employee_id=?", (eid,))
    assert [m["how"] for m in raw] == ["메일(인사기록카드)"]
    import sqlite3
    from app.config import settings
    stored = sqlite3.connect(settings.db_path).execute("SELECT to_addr, body FROM offboard_mails WHERE employee_id=?", (eid,)).fetchone()
    assert stored[0].startswith("enc:v1:") and stored[1].startswith("enc:v1:")          # 주소·본문은 암호화 저장
    assert "b1" in offboard.checks(eid)
    page = hr.get(f"/hr/offboard/{eid}").text
    assert '본문 <b>굵게</b><span style="color: red">색</span>' in page and "xss_mark" not in page   # 허용한 모양만 남음
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
    assert not offboard.templates_text()["custom"] and offboard.templates_text()["body"] == richtext.from_text(offboard.DEFAULT_BODY)
    # 편집기로 쓴 글귀: 허용한 모양만 저장, 이름은 글자 그대로 들어감
    hr.post("/hr/offboard/template", data={"subject": "안내", "body": '<b>{이름}</b>님<script>xss_mark_4()</script><a href="javascript:xss_mark_5()">링크</a>'})
    assert offboard.templates_text()["body"] == "<b>{이름}</b>님링크"
    e2 = _emp("<i>기울임</i>", email="")
    page = hr.get(f"/hr/offboard/{e2}").text
    assert "<b>&lt;i&gt;기울임&lt;/i&gt;</b>님링크" in page and "xss_mark" not in page
    hr.post("/hr/offboard/template", data={"subject": "안내", "body": "<div><br></div>"})        # 빈 글은 저장 안 됨
    assert offboard.templates_text()["body"] == "<b>{이름}</b>님링크"
    hr.post("/hr/offboard/template", data={"reset": "1", "subject": "x", "body": "y"})
    # 예전에 글자만으로 저장한 글귀는 그대로 이어서 쓴다
    set_setting("offboard_body", "안녕하세요\n1. 제목\n - 할 일 <b>")
    t = offboard.templates_text()
    assert t["custom"] and "<li>할 일 &lt;b&gt;</li>" in t["body"] and "1. 제목</span>" in t["body"]
    set_setting("offboard_body", "")
    # 조회자·총무 담당은 접근 불가
    with TestClient(app):
        for user, role in (("ob_view", "viewer"), ("ob_ga", "ga")):
            c = _client(user, role)
            assert c.get("/hr/offboard", follow_redirects=False).status_code == 403
            assert c.get(f"/hr/offboard/{eid}", follow_redirects=False).status_code == 403
            assert c.post(f"/hr/offboard/{eid}/send", data={"target": "hand", "subject": "a", "body": "b"}, follow_redirects=False).status_code == 403
            assert "/hr/offboard" not in c.get(f"/hr/employees/{eid}").text


def test_offboard_stages_edit(hr):
    eid = _emp("퇴사단계시험")
    hr.post(f"/hr/offboard/{eid}/save", data={"last_day": "2027-03-31"})
    assert hr.post(f"/hr/offboard/{eid}/check", json={"key": "a1", "done": True}).json()["done"] == 1
    base = [{"k": sk, "name": n, "when": w, "items": [{"k": k, "text": t, "hint": h} for k, t, h in items]} for sk, n, w, items in offboard.STAGES]
    assert not offboard.stages_custom()
    try:
        # 글자 고치기 + 항목 추가 + 항목 삭제(a2) + 단계 추가. 있던 키는 그대로라 체크가 이어진다
        new = [dict(s, items=list(s["items"])) for s in base]
        new[0]["items"][0] = {"k": "a1", "text": "사직서 <b>받기</b>", "hint": "고친 참고"}
        del new[0]["items"][1]
        new[0]["items"].append({"k": "", "text": "새 항목", "hint": ""})
        new[0]["items"].append({"k": "", "text": "   ", "hint": "할 일이 빈 줄은 버림"})
        new.append({"k": "a", "name": "추가 단계", "when": "", "items": [{"k": "a1", "text": "키가 겹치면 새로 만든다", "hint": ""}]})
        r = hr.post("/hr/offboard/stages", json={"stages": new})
        assert r.status_code == 200 and offboard.stages_custom()
        st = offboard.stages()
        assert len(st) == len(base) + 1 and st[0][3][0] == ("a1", "사직서 <b>받기</b>", "고친 참고")
        keys = offboard.keys()
        assert "a2" not in keys and len(keys) == len(set(keys)) == len(offboard.KEYS_DEFAULT) + 1
        assert st[-1][0] != "a" and st[-1][3][0][0] != "a1"
        page = hr.get(f"/hr/offboard/{eid}").text
        assert "사직서 &lt;b&gt;받기&lt;/b&gt;" in page and "새 항목" in page and "추가 단계" in page
        assert hr.post(f"/hr/offboard/{eid}/check", json={"key": "a2", "done": True}).status_code == 400   # 지운 항목
        assert hr.post(f"/hr/offboard/{eid}/check", json={"key": st[0][3][-1][0], "done": True}).json()["done"] == 2
        assert "수정한 단계 사용 중" in hr.get("/hr/offboard").text
        # 잘못된 값은 거절하고 그대로 둔다
        for bad in ([], "x", [{"name": "", "items": []}], [{"name": "이름만", "items": []}], [{"name": "n", "items": [{"text": "t"}]}] * 13):
            assert hr.post("/hr/offboard/stages", json={"stages": bad}).status_code == 400
        assert len(offboard.stages()) == len(base) + 1
        with TestClient(app):
            assert _client("ob_view2", "viewer").post("/hr/offboard/stages", json={"reset": True}).status_code == 403
    finally:
        assert hr.post("/hr/offboard/stages", json={"reset": True}).status_code == 200
    assert not offboard.stages_custom() and offboard.stages() == offboard.STAGES


def test_richtext_clean():
    c = richtext.clean
    assert c('<p onclick="x()">a</p>') == "<p>a</p>"
    assert c("<script>x()</script>글<style>p{}</style>") == "글"
    assert c('<a href="javascript:x()">링크</a><img src=x onerror=x()><iframe src="//e"></iframe>') == "링크"
    assert c('<span style="color: #1f4e79; font-size: x-large; background: url(x); position: fixed">가</span>') == \
        '<span style="color: #1f4e79; font-size: x-large">가</span>'
    assert c('<span style="color: expression(x)">가</span>') == "<span>가</span>"
    assert c('<font color="red" size="5" face="x">가</font>') == '<font color="red" size="5">가</font>'
    assert c("<b><i>닫지 않음") == "<b><i>닫지 않음</i></b>"
    assert c("1 < 2 & 3") == "1 &lt; 2 &amp; 3"
    assert richtext.to_text("<div>가</div><ul><li>나</li></ul><div>다&amp;라</div>") == "가\n - 나\n\n다&라"
