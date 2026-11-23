"""두 회사(본사·자회사) 구분, 회사별 문서·로봇, 문서 보관함."""
import os
import re

import pytest
from fastapi.testclient import TestClient

from app import archive, brand, company
from app.db import execute, get_settings, query, query_one, set_setting
from app.main import app
from app.modules.hr import ecount_robot as er, repo, services
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def _login(c, user):
    c.post("/login", data={"username": user, "password": "Strong-Pass-123!"}, headers=ORIGIN)
    c.headers.update(ORIGIN)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='co_hr'"):
            auth.create_user("co_hr", "인사", "hr", "Strong-Pass-123!", must_change=False)
            auth.create_user("co_admin", "관리", "admin", "Strong-Pass-123!", must_change=False)
            auth.create_user("co_view", "조회", "viewer", "Strong-Pass-123!", must_change=False)
        _login(c, "co_hr")
        yield c


@pytest.fixture()
def two(client):
    """자회사를 켜고, 시험이 끝나면 만든 자료를 지우고 다시 끔 (다른 시험은 한 회사로 돌게)."""
    for k, v in {"co2_company_name": "(주)둘째회사", "co2_ceo_name": "김둘째", "co2_company_address": "부산시 어딘가 2",
                 "co2_business_no": "222-22-22222", "co2_company_short": "둘째"}.items():
        set_setting(k, v)
    made = []
    yield made
    for eid in made:
        repo.delete_employee(eid)
    for k in ("co2_company_name", "co2_ceo_name", "co2_company_address", "co2_business_no", "co2_company_short",
              "co2_ecount_robot_key", "co2_ecount_depts", "co2_ecount_dept_map", "co2_doc_seal"):
        set_setting(k, "")
    client.cookies.delete(company.COOKIE)


def _emp(made, name, co, **kw):
    eid = repo.create_employee({"name": name, "hire_date": "2026-10-01", "department": "경영지원팀", "rank": "사원",
                                "employment_type": "정규직", "company": co, "gender": "여", **kw})
    made.append(eid)
    return eid


def test_off_by_default(client):
    """자회사 이름이 없으면 탭도 회사 칸도 없고 예전과 같다."""
    assert not company.enabled() and company.cur() == "all" and company.sql("e") == ""
    page = client.get("/hr/employees").text
    assert 'class="co-tabs' not in page and 'name="company"' not in client.get("/hr/employees/new").text
    assert "이카운트 연동" in page and "입사서류 등록 초안" not in page     # 초안 기능은 서류 폴더를 정했을 때만


def test_scope_tabs_and_lists(client, two):
    a = _emp(two, "본사직원가", "1")
    b = _emp(two, "자회사직원나", "2")
    assert company.of_employee(a) == "1" and company.of_employee(b) == "2"
    page = client.get("/hr/employees").text                       # 통합: 둘 다, 탭과 회사 배지
    assert 'class="co-tabs' in page and "본사직원가" in page and "자회사직원나" in page and "둘째" in page
    page = client.get("/hr/employees?co=2").text                  # 자회사만
    assert "자회사직원나" in page and "본사직원가" not in page
    assert client.cookies.get(company.COOKIE) == "2"
    assert "본사직원가" not in client.get("/hr/employees").text    # 고른 회사는 기억됨
    assert "본사직원가" not in client.get("/hr/health").text and "자회사직원나" in client.get("/hr/health").text
    # 새로 등록하면 지금 보는 회사가 기본값
    assert re.search(r'<option value="2" selected>', client.get("/hr/employees/new").text)
    # 경영관리 화면은 회사 탭의 영향을 받지 않음
    assert 'class="co-tabs' not in client.get("/mgmt/review").text
    page = client.get("/hr/employees?co=1").text
    assert "본사직원가" in page and "자회사직원나" not in page
    # 한꺼번에 회사 옮기기
    r = client.post("/hr/employees/company", data={"ids": [str(a)], "company": "2"}, follow_redirects=False)
    assert r.status_code == 303 and company.of_employee(a) == "2"
    client.get("/hr/employees?co=all")
    # 대시보드: 회사별 요약 세 줄
    rows = services.company_summary()
    assert [r["id"] for r in rows] == ["all", "1", "2"] and rows[2]["active"] >= 2 and rows[0]["active"] == rows[1]["active"] + rows[2]["active"]
    assert "회사별 인원 현황" in client.get("/hr/dashboard").text and "회사별 인원 현황" in client.get("/").text


def test_documents_use_employee_company(client, two):
    b = _emp(two, "자회사문서다", "2")
    emp = repo.get_employee(b)
    ctx = services.build_context(emp, {})
    assert ctx["회사명"] == "(주)둘째회사" and ctx["대표자"] == "김둘째" and ctx["사업자번호"] == "222-22-22222"
    a = _emp(two, "본사문서라", "1")
    assert services.build_context(repo.get_employee(a), {})["회사명"] == get_settings()["company_name"]
    assert company.name_for(emp) == "(주)둘째회사" and company.name_for(a) == get_settings()["company_name"]
    # 도장: 회사마다 따로
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 0, 0)).save(buf, "PNG")
    assert brand.save("seal", buf.getvalue(), "2") == ""
    assert brand.get("seal", "2") and "c=2" in brand.css(False, "2") and "c=2" not in brand.css(False, "1")
    # 회사 전용 템플릿은 그 회사 직원에게만
    tid = repo.save_template(None, "certificate", "재직증명", "둘째 전용 재직증명", "<p>{{회사명}} {{성명}}</p>", 1, "2")
    try:
        assert [t["id"] for t in repo.list_templates("certificate", co="1") if t["id"] == tid] == []
        assert repo.list_templates("certificate", co="2")[0]["id"] == tid                 # 전용이 먼저
        r = client.post("/hr/certificates/new", data={"employee_id": a, "template_id": tid, "purpose": "시험"}, follow_redirects=False)
        assert r.status_code == 303 and "/hr/certificates" in r.headers["location"] and "documents" not in r.headers["location"]
        r = client.post("/hr/certificates/new", data={"employee_id": b, "template_id": tid, "purpose": "시험"}, follow_redirects=False)
        assert "/hr/documents/" in r.headers["location"]
        doc_id = int(r.headers["location"].split("/hr/documents/")[1].split("?")[0])
        assert "(주)둘째회사" in repo.get_document(doc_id)["content"]
        assert "doc-asset/seal?c=2" in client.get(f"/hr/documents/{doc_id}").text
    finally:
        execute("DELETE FROM hr_documents WHERE template_id=?", (tid,))
        repo.delete_template(tid)


def test_robot_per_company(client, two):
    a = _emp(two, "본사로봇마", "1", birth_date="1990-01-01")
    b = _emp(two, "자회사로봇바", "2", birth_date="1991-01-01")
    k1, k2 = er.new_key("1"), er.new_key("2")
    assert er.key_company(k1) == "1" and er.key_company(k2) == "2" and er.key_company("x") == ""
    assert er.request([(a, "신규"), (b, "신규")], "t") == 2
    j2 = client.post("/robot/ecount/claim", json={}, headers={"x-robot-key": k2}).json()["job"]
    assert j2["values"]["name"] == "자회사로봇바"                                       # 자회사 열쇠는 자회사 직원만
    assert client.post("/robot/ecount/claim", json={}, headers={"x-robot-key": k2}).json()["job"] is None
    j1 = client.post("/robot/ecount/claim", json={}, headers={"x-robot-key": k1}).json()["job"]
    assert j1["values"]["name"] == "본사로봇마"
    # 남의 회사 일감은 끝낼 수 없음
    bad = client.post("/robot/ecount/finish", json={"job_id": j1["job_id"], "ok": True, "detail": {"saved": True}}, headers={"x-robot-key": k2})
    assert bad.json()["ok"] is False
    assert client.post("/robot/ecount/finish", json={"job_id": j1["job_id"], "ok": True, "detail": {"saved": True}}, headers={"x-robot-key": k1}).json()["ok"]
    # 부서 목록·짝도 회사마다
    client.post("/robot/ecount/depts", json={"depts": [["B01", "둘째 관리부"]]}, headers={"x-robot-key": k2})
    assert er.ecount_depts("2") == [["B01", "둘째 관리부"]] and ["B01", "둘째 관리부"] not in er.ecount_depts("1")
    client.post("/hr/ecount/robot/settings", data={"rc": "2", "dept:경영지원팀": "B01", "pay_std": "00002", "ok_confirms": ""})
    assert er.dept_code_for("경영지원팀", "2") == "B01" and er.pay_std("2") == "00002" and er.pay_std("1") != "00002" or er.pay_std("1") == "00001"
    assert er.status("2")["online"]
    execute("DELETE FROM ecount_robot_jobs WHERE employee_id IN (?,?)", (a, b))
    set_setting("co2_ecount_pay_std", "")
    set_setting("co2_ecount_ok_confirms", "")


def test_ga_company_column(client, two):
    from app.modules.ga import repo as ga
    v1 = ga.save("ga_vehicles", None, {"plate_no": "11가1111", "model": "본사차", "status": "운행", "company": "1"})
    v2 = ga.save("ga_vehicles", None, {"plate_no": "22나2222", "model": "자회사차", "status": "운행", "company": "2"})
    try:
        page = client.get("/ga/vehicles?co=2").text
        assert "22나2222" in page and "11가1111" not in page and 'name="company"' in page
        page = client.get("/ga/vehicles?co=1").text
        assert "11가1111" in page and "22나2222" not in page
        client.get("/ga/vehicles?co=all")
        ga.save("ga_vehicles", v2, {"memo": "고침"})                       # 회사 칸 없이 고쳐도 회사는 그대로
        assert query_one("SELECT company FROM ga_vehicles WHERE id=?", (v2,))["company"] == "2"
    finally:
        execute("DELETE FROM ga_vehicles WHERE id IN (?,?)", (v1, v2))


def test_archive_flow(client, two):
    from app.modules.ga import repo as ga
    vid = ga.save("ga_vehicles", None, {"plate_no": "33다3333", "model": "보관함차", "status": "운행", "company": "2"})
    fid = execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount) VALUES (?,?,?,?)", (vid, "2026-11-01 10:00", "주정차", 40000))
    try:
        page = client.get(f"/files/ga.fines/{fid}").text
        assert "과태료" in page and "33다3333" in page and "둘째" in page
        up = lambda name, raw: client.post(f"/files/ga.fines/{fid}/upload", files=[("files", (name, raw, "application/octet-stream"))],
                                           data={"note": "고지서"}, follow_redirects=False)
        r = up("고지서 원본.pdf", PDF)
        assert r.status_code == 303
        att = archive.items("ga.fines", fid)[0]
        assert att["orig_name"] == "고지서 원본.pdf" and att["company"] == "2"
        path = os.path.join(archive.root(), att["rel_path"])
        assert os.path.exists(path) and "둘째/총무관리/법인차량/33다3333 보관함차/과태료/2026-11-01_" in att["rel_path"]
        got = client.get(f"/files/get/{att['id']}")
        assert got.status_code == 200 and got.content == PDF and got.headers["x-content-type-options"] == "nosniff"
        # 이름만 바꾼 파일·허용하지 않는 종류는 거절
        up("악성.pdf", b"MZ\x90\x00 not a pdf")
        up("스크립트.html", b"<script>1</script>")
        up("../../밖.pdf", PDF)                                              # 경로 글자는 이름에서 지워짐
        rows = archive.items("ga.fines", fid)
        assert len(rows) == 2 and all(".." not in r["rel_path"] and r["rel_path"].startswith("둘째/") for r in rows)
        # 폴더에서 파일이 바뀌면 내주지 않음
        with open(path, "ab") as f:
            f.write(b"x")
        assert client.get(f"/files/get/{att['id']}").status_code == 409
        # 조회자는 볼 수 없음
        with TestClient(app) as v:
            _login(v, "co_view")
            assert v.get(f"/files/ga.fines/{fid}").status_code == 403 and v.get(f"/files/get/{att['id']}").status_code == 404
        # 지우면 삭제함으로
        assert client.post(f"/files/del/{att['id']}", follow_redirects=False).status_code == 303
        assert not os.path.exists(path) and os.path.exists(os.path.join(archive.root(), archive.TRASH, att["rel_path"]))
        assert len(archive.items("ga.fines", fid)) == 1
        assert client.get("/files/nope/1").status_code == 403
    finally:
        execute("DELETE FROM attachments WHERE menu='ga.fines' AND ref_id=?", (fid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


def test_contracts_and_items_merged(client, two, monkeypatch):
    """계약·임대차와 회사 관리 만기를 한 화면으로, 계약에도 만기 알림 메일."""
    from datetime import timedelta
    from app.common import today
    from app.modules.ga import company as items, repo as ga
    t = today()
    cid = ga.save("ga_contracts", None, {"category": "임대차", "title": "합친화면 임대차", "counterparty": "건물주", "status": "유효",
                                         "start_date": (t - timedelta(days=300)).isoformat(), "end_date": (t + timedelta(days=50)).isoformat(),
                                         "amount": 1000000, "pay_cycle": "월", "notice_days": 30, "company": "1"})
    iid = items.save({"category": "인증·확인서", "name": "합친화면 인증서", "expire_date": (t + timedelta(days=10)).isoformat(), "company": "2"})
    try:
        page = client.get("/ga/contracts?co=all").text
        assert "합친화면 임대차" in page and "합친화면 인증서" in page and "계약·만기 관리" in page
        assert page.index("합친화면 인증서") < page.index("합친화면 임대차")            # 만기 가까운 순
        assert page.count('name="alert_email"') >= 2                                  # 두 등록 창 모두 알림 메일 칸
        assert "합친화면 임대차" not in client.get("/ga/contracts?kind=item").text
        assert "합친화면 인증서" not in client.get("/ga/contracts?kind=contract").text
        r = client.get("/ga/company", follow_redirects=False)                          # 예전 주소는 합친 화면으로
        assert r.status_code == 303 and r.headers["location"].startswith("/ga/contracts")
        assert "회사 관리 만기" not in client.get("/ga/dashboard").text.split("side-nav")[1].split("</nav>")[0]
        # 계약에 알림 메일: 잘못된 주소는 거절, 맞으면 저장
        client.post("/ga/contracts/save", data={"id": str(cid), "title": "합친화면 임대차", "counterparty": "건물주", "alert_email": "bad"})
        assert not query_one("SELECT alert_email FROM ga_contracts WHERE id=?", (cid,))["alert_email"]
        client.post("/ga/contracts/save", data={"id": str(cid), "title": "합친화면 임대차", "counterparty": "건물주", "alert_email": "a@example.com, b@example.com"})
        assert query_one("SELECT alert_email FROM ga_contracts WHERE id=?", (cid,))["alert_email"] == "a@example.com, b@example.com"
        # 통보기한(만료 30일 전)까지 20일 → 지금 보낼 대상, 보낸 뒤에는 같은 단계 다시 안 보냄
        due = [c for c in items.contract_due_mails() if c["id"] == cid]
        assert len(due) == 1 and due[0]["stage"] == "first" and due[0]["target_dday"] == 20
        sent = []
        from app import mailer
        monkeypatch.setattr(mailer, "configured", lambda: True)
        monkeypatch.setattr(mailer, "send", lambda to, subject, body, *a, **k: sent.append((to, subject, body)))
        assert items.send_due_mails() >= 1
        mine = [x for x in sent if "합친화면 임대차" in x[1]]
        assert {x[0] for x in mine} == {"a@example.com", "b@example.com"} and "통보기한" in mine[0][2]
        assert not [c for c in items.contract_due_mails() if c["id"] == cid]
    finally:
        execute("DELETE FROM ga_contracts WHERE id=?", (cid,))
        items.delete(iid)
        client.get("/ga/contracts?co=all")


def test_vehicle_folder_name_and_rehome(client, two):
    """차량 폴더는 '차량번호 차종'. 차종을 고치면 붙인 서류가 새 폴더로 옮겨짐."""
    from app.modules.ga import repo as ga
    vid = ga.save("ga_vehicles", None, {"plate_no": "44라4444", "model": "시험차종A", "status": "운행", "company": "1"})
    try:
        aid = archive.save("ga.vehicles", vid, "등록증.pdf", PDF)
        att = archive.get(aid)
        assert "/법인차량/44라4444 시험차종A/" in att["rel_path"]
        ga.save("ga_vehicles", vid, {"model": "시험차종B"})
        assert archive.rehome() == 1
        att2 = archive.get(aid)
        assert "/법인차량/44라4444 시험차종B/" in att2["rel_path"] and archive.read(att2) == PDF
        assert not os.path.exists(os.path.join(archive.root(), os.path.dirname(att["rel_path"])))    # 빈 예전 폴더는 지움
        assert archive.rehome() == 0
    finally:
        execute("DELETE FROM attachments WHERE menu='ga.vehicles' AND ref_id=?", (vid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


def test_generated_pdf_goes_to_archive(two, monkeypatch, tmp_path):
    """문서 보관함을 정해 두면 계약서·증명서 PDF 도 그 직원의 '임직원 서류' 폴더로."""
    from app import storage
    from app.config import settings
    a = _emp(two, "보관함문서사", "2")
    emp = repo.get_employee(a)
    doc = {"id": 7, "employee_id": a, "doc_group": "certificate", "category": "재직증명", "name": emp["name"], "emp_no": emp["emp_no"]}
    assert storage.archive_path(doc) is None                                  # 정하지 않았으면 예전 위치 그대로
    monkeypatch.setattr(settings, "archive_root", str(tmp_path))
    p = storage.target_path(doc)
    rel = os.path.relpath(p, str(tmp_path)).replace("\\", "/")
    assert rel.startswith(f"둘째/인사관리/임직원 서류/{emp['emp_no']}_보관함문서사/BMS_증명서_재직증명_보관함문서사_") and rel.endswith("_d7.pdf")
    os.makedirs(os.path.dirname(p))
    open(p, "wb").write(PDF)
    import hashlib
    assert storage.read_verified(p, hashlib.sha256(PDF).hexdigest()) == PDF


def test_no_duplicate_folders(client, two):
    """손으로 만든 폴더·미리 넣어 둔 파일이 있어도 폴더가 두 벌 생기지 않는다."""
    from app.modules.ga import repo as ga
    car_parent = os.path.join(archive.root(), company.short("1"), "총무관리", "법인차량")
    os.makedirs(os.path.join(car_parent, "55마5555"), exist_ok=True)                 # 담당자가 차량번호만으로 먼저 만든 폴더
    with open(os.path.join(car_parent, "55마5555", "미리 넣은 등록증.pdf"), "wb") as f:
        f.write(PDF)
    with open(os.path.join(car_parent, "55마5555", "가짜.pdf"), "wb") as f:
        f.write(b"not a pdf")
    r = client.post("/ga/vehicles/save", data={"plate_no": "55마5555", "model": "미리차", "status": "운행", "company": "1"}, follow_redirects=False)
    vid = int(r.headers["location"].split("/ga/vehicles/")[1].split("?")[0])
    try:
        dirs = [d for d in os.listdir(car_parent) if d.startswith("55마5555")]
        assert dirs == ["55마5555 미리차"]                                           # 새로 만들지 않고 이름을 바꿔 씀
        page = client.get(f"/files/ga.vehicles/{vid}").text
        assert "대장에 없는 파일" in page and "미리 넣은 등록증.pdf" in page
        assert [x["name"] for x in archive.unregistered("ga.vehicles", vid)] == ["가짜.pdf", "미리 넣은 등록증.pdf"]
        r = client.post(f"/files/ga.vehicles/{vid}/adopt", data={"names": ["미리 넣은 등록증.pdf", "가짜.pdf", "../밖.pdf"]}, follow_redirects=False)
        assert r.status_code == 303
        rows = archive.items("ga.vehicles", vid)
        assert len(rows) == 1 and rows[0]["rel_path"].endswith("/법인차량/55마5555 미리차/미리 넣은 등록증.pdf")   # 복사 없이 그 자리 그대로
        assert client.get(f"/files/get/{rows[0]['id']}").content == PDF
        assert [x["name"] for x in archive.unregistered("ga.vehicles", vid)] == ["가짜.pdf"]      # 내용이 PDF 가 아닌 것은 올리지 않음
        archive.save("ga.vehicles", vid, "새 서류.pdf", PDF)
        assert [d for d in os.listdir(car_parent) if d.startswith("55마5555")] == ["55마5555 미리차"]
        assert archive.ensure_all() >= 1 and archive.rehome() == 0
    finally:
        execute("DELETE FROM attachments WHERE menu='ga.vehicles' AND ref_id=?", (vid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))
