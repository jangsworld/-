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
    set_setting("archive_auto", "0")          # 여기서는 NAS 파일을 사람이 올리는 방식을 확인 (자동은 test_explorer)
    yield made
    set_setting("archive_auto", "")
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
        assert os.path.exists(path) and "둘째/총무관리/법인차량/33다3333 보관함차/과태료/2026-11-01 주정차/2026-11-01_" in att["rel_path"]
        got = client.get(f"/files/get/{att['id']}")
        assert got.status_code == 200 and got.content == PDF and got.headers["x-content-type-options"] == "nosniff"
        # 이름만 바꾼 파일·허용하지 않는 종류는 거절
        up("악성.pdf", b"MZ\x90\x00 not a pdf")
        up("스크립트.html", b"<script>1</script>")
        up("../../밖.pdf", PDF)                                              # 경로 글자는 이름에서 지워짐
        rows = archive.items("ga.fines", fid)
        assert len(rows) == 2 and all(".." not in r["rel_path"] and r["rel_path"].startswith("둘째/") for r in rows)
        # 폴더에서 파일이 종류와 다른 내용으로 바뀌면 내주지 않음 (제대로 고친 파일은 test_explorer 에서 반영됨을 확인)
        with open(path, "wb") as f:
            f.write(b"ENCRYPTED")
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
        old_dir = os.path.dirname(att["rel_path"])
        assert not os.path.exists(os.path.join(archive.root(), old_dir))                        # 폴더째 새 이름으로 바뀜 (두 벌이 되지 않음)
        assert archive.rehome() == 0
        # 새 이름의 폴더가 이미 있으면 파일만 옮기고, 비게 된 예전 폴더는 지우지 않고 '_빈폴더' 로 옮겨 둠
        ga.save("ga_vehicles", vid, {"model": "시험차종C"})
        new_dir = os.path.dirname(att2["rel_path"]).replace("시험차종B", "시험차종C")
        os.makedirs(os.path.join(archive.root(), new_dir))
        assert archive.rehome() == 1
        b_dir = os.path.dirname(att2["rel_path"])
        assert archive.read(archive.get(aid)) == PDF and "시험차종C" in archive.get(aid)["rel_path"]
        assert not os.path.exists(os.path.join(archive.root(), b_dir))
        assert os.path.isdir(os.path.join(archive.root(), archive.EMPTY, b_dir))                  # 원래 경로 그대로 보관
        assert os.path.isdir(os.path.join(archive.root(), os.path.dirname(b_dir)))                # 메뉴 폴더는 그대로
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


def test_register_with_files_search_and_codes(client, two):
    """등록 창에서 바로 서류 붙이기, 담당자 찾기 칸, 면허·보험·인증 구분을 구분값으로, 차량 폴더는 차종까지만."""
    from app.db import get_codes
    from app.modules.ga import company as items
    assert archive.vehicle_name("12가3456", "샘플차종_홍길동") == "12가3456 샘플차종" and archive.vehicle_name("12가3456", "") == "12가3456"
    assert archive.vehicle_name("12가3456_홍길동", "샘플 차종") == "12가3456 샘플 차종" and archive._same_thing("12가3456_홍길동 샘플 차종", "12가3456")
    page = client.get("/ga/contracts?co=all").text
    assert 'name="manager_id"' in page and "data-search" in page.split('name="manager_id"')[1][:200]
    assert page.count('name="files" multiple') == 2 and page.count('enctype="multipart/form-data"') >= 2
    # 계약 등록과 동시에 계약서 붙이기
    r = client.post("/ga/contracts/save", data={"category": "임대차", "title": "첨부동시 계약", "counterparty": "임대인", "status": "유효", "company": "1"},
                    files=[("files", ("계약서 스캔.pdf", PDF, "application/pdf")), ("files", ("나쁜.pdf", b"zz", "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303
    cid = query_one("SELECT id FROM ga_contracts WHERE title='첨부동시 계약'")["id"]
    iid = None
    try:
        rows = archive.items("ga.contracts", cid)
        assert len(rows) == 1 and rows[0]["orig_name"] == "계약서 스캔.pdf" and "/계약·만기 관리/첨부동시 계약/" in rows[0]["rel_path"]
        # 면허·보험·인증도 등록과 동시에
        r = client.post("/ga/company/save", data={"category": "보험·보증", "name": "첨부동시 보험", "expire_date": "2027-03-01", "company": "1"},
                        files=[("files", ("증권.pdf", PDF, "application/pdf"))], follow_redirects=False)
        assert r.status_code == 303
        iid = query_one("SELECT id FROM ga_company_items WHERE name='첨부동시 보험'")["id"]
        assert len(archive.items("ga.company", iid)) == 1
        # 구분값: 설정에서 고친 목록을 씀
        assert "보험·보증" in get_codes("COMPANY_ITEM") and items.categories() == get_codes("COMPANY_ITEM")
        execute("INSERT INTO codes(group_code, name, sort_order) VALUES ('COMPANY_ITEM', '새구분시험', 99)")
        assert "새구분시험" in client.get("/ga/contracts").text
        iid2 = items.save({"category": "새구분시험", "name": "새구분 항목"})
        items.delete(iid2)
        with pytest.raises(items.CompanyError):
            items.save({"category": "없는구분", "name": "x"})
    finally:
        execute("DELETE FROM codes WHERE group_code='COMPANY_ITEM' AND name='새구분시험'")
        execute("DELETE FROM attachments WHERE menu IN ('ga.contracts','ga.company') AND ref_id IN (?,?)", (cid, iid or 0))
        execute("DELETE FROM ga_contracts WHERE id=?", (cid,))
        if iid:
            items.delete(iid)


def test_subfolders_are_recognized(client, two):
    """대상 폴더 안에 담당자가 만든 하위 폴더의 파일도 대장에 올릴 수 있고, 대상 이름이 바뀌어도 하위 폴더가 유지된다."""
    from app.modules.ga import repo as ga
    cid = ga.save("ga_contracts", None, {"category": "임대차", "title": "하위폴더 계약", "counterparty": "임대인", "status": "유효", "company": "1"})
    vid = ga.save("ga_vehicles", None, {"plate_no": "66바6666", "model": "하위차", "status": "운행", "company": "1"})
    try:
        base = os.path.join(archive.root(), archive.ensure_folder("ga.contracts", cid))
        os.makedirs(os.path.join(base, "기타서류", "2024"))
        for rel in ("본계약.pdf", "기타서류/확인서.pdf", "기타서류/2024/영수증.pdf", "기타서류/메모.txt", "기타서류/BMS_자동.pdf"):
            with open(os.path.join(base, *rel.split("/")), "wb") as f:
                f.write(PDF)
        names = [x["name"] for x in archive.unregistered("ga.contracts", cid)]
        assert names == ["기타서류/2024/영수증.pdf", "기타서류/확인서.pdf", "본계약.pdf"]
        page = client.get(f"/files/ga.contracts/{cid}").text
        assert "대장에 없는 파일" in page and "확인서.pdf" in page and "기타서류" in page
        client.post(f"/files/ga.contracts/{cid}/adopt", data={"names": names + ["기타서류/../../밖.pdf"]})
        rows = {r["rel_path"].split("/하위폴더 계약/")[1]: r for r in archive.items("ga.contracts", cid)}
        assert set(rows) == set(names) and rows["기타서류/2024/영수증.pdf"]["subdir"] == "기타서류/2024" and rows["본계약.pdf"]["subdir"] is None
        assert client.get(f"/files/get/{rows['기타서류/확인서.pdf']['id']}").content == PDF
        assert archive.unregistered("ga.contracts", cid) == [] and archive.rehome() == 0         # 하위 폴더 파일을 위로 끌어올리지 않음
        ga.save("ga_contracts", cid, {"title": "하위폴더 계약 새이름"})
        assert archive.rehome() == 3
        moved = archive.get(rows["기타서류/2024/영수증.pdf"]["id"])
        assert moved["rel_path"].endswith("/하위폴더 계약 새이름/기타서류/2024/영수증.pdf") and archive.read(moved) == PDF
        assert not os.path.exists(base)                                                         # 폴더째 새 이름으로: 대장에 없는 파일도 함께 따라감
        assert os.path.exists(os.path.join(base + " 새이름", "기타서류", "메모.txt"))
        assert not os.path.exists(os.path.join(archive.root(), archive.EMPTY, company.short("1"), "총무관리", "계약·만기 관리", "하위폴더 계약"))
        # 차량 서류 화면은 과태료·사고·점검사진 폴더(다른 메뉴 것)를 건너뜀
        vbase = os.path.join(archive.root(), archive.ensure_folder("ga.vehicles", vid))
        for d in ("과태료", "보험"):
            os.makedirs(os.path.join(vbase, d))
            with open(os.path.join(vbase, d, "문서.pdf"), "wb") as f:
                f.write(PDF)
        assert [x["name"] for x in archive.unregistered("ga.vehicles", vid)] == ["보험/문서.pdf"]
    finally:
        execute("DELETE FROM attachments WHERE (menu='ga.contracts' AND ref_id=?) OR (menu='ga.vehicles' AND ref_id=?)", (cid, vid))
        execute("DELETE FROM ga_contracts WHERE id=?", (cid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


def test_new_file_notice_and_detail_pages(client, two):
    """폴더에 새로 넣은 파일 알림(목록 위 카드·이름 옆 배지), 계약·면허 상세 화면(지금 상태·이력·서류), 차량 상세의 서류 묶음."""
    from app.modules.ga import company as items_mod
    from app.modules.ga import repo as ga
    cid = ga.save("ga_contracts", None, {"category": "임대차", "title": "알림시험 계약", "counterparty": "임대인A", "status": "유효",
                                          "company": "1", "end_date": "2031-12-31", "amount": 1100000, "pay_cycle": "월"})
    iid = items_mod.save({"category": items_mod.categories()[0], "name": "알림시험 인증", "expire_date": "2031-06-30", "status": "유효", "company": "1"}, "시험")
    vid = ga.save("ga_vehicles", None, {"plate_no": "77사7777", "model": "알림차", "status": "운행", "company": "1"})
    try:
        archive.pending_clear()
        assert "pendingCard" not in client.get("/ga/contracts?co=all").text
        cbase = os.path.join(archive.root(), archive.ensure_folder("ga.contracts", cid))
        ibase = os.path.join(archive.root(), archive.ensure_folder("ga.company", iid))
        vbase = os.path.join(archive.root(), archive.ensure_folder("ga.vehicles", vid))
        os.makedirs(os.path.join(cbase, "변경계약"))
        for path in (os.path.join(cbase, "원본.pdf"), os.path.join(cbase, "변경계약", "1차.pdf"), os.path.join(ibase, "인증서.pdf"),
                     os.path.join(vbase, "등록증.pdf")):
            with open(path, "wb") as f:
                f.write(PDF)
        archive.pending_clear()
        pend = {(p["menu"], p["ref_id"]): p for p in archive.pending(["ga.contracts", "ga.company", "ga.vehicles"])}
        assert pend[("ga.contracts", cid)]["n"] == 2 and pend[("ga.company", iid)]["n"] == 1 and pend[("ga.vehicles", vid)]["n"] == 1
        assert pend[("ga.contracts", cid)]["url"] == f"/ga/contracts/{cid}#files"
        # 목록: 알림 카드 + 이름 옆 배지 + 이름을 누르면 상세로
        page = client.get("/ga/contracts?co=all").text
        assert "pendingCard" in page and "폴더에 새로 넣은 파일이 있습니다" in page and "확인하고 올리기" in page
        assert f'href="/ga/contracts/{cid}">알림시험 계약</a>' in page and f'href="/ga/contracts/item/{iid}">알림시험 인증</a>' in page
        assert "새 파일 2" in page and f'value="ga.contracts:{cid}"' in page
        vpage = client.get("/ga/vehicles?co=all").text
        assert "pendingCard" in vpage and f'value="ga.vehicles:{vid}"' in vpage
        # 상세: 지금 상태 · 이력 · 서류(대장에 없는 파일 포함)
        d = client.get(f"/ga/contracts/{cid}")
        assert d.status_code == 200
        for word in ("지금 상태", "이력", "임대인A", "2031-12-31", "폴더에 있는데 대장에 없는 파일", "원본.pdf", "변경계약", "내용 고치기", "contractModal",
                     f'name="next" value="/ga/contracts/{cid}"'):
            assert word in d.text, word
        di = client.get(f"/ga/contracts/item/{iid}")
        assert di.status_code == 200 and "지금 상태" in di.text and "인증서.pdf" in di.text and "itemModal" in di.text
        dv = client.get(f"/ga/vehicles/{vid}").text
        assert "차량 서류" in dv and "등록증.pdf" in dv and 'id="files"' in dv
        assert client.get("/ga/contracts/999999").status_code == 404 and client.get("/ga/contracts/item/999999").status_code == 404
        assert client.get("/ga/contracts/export.csv").status_code == 200                       # 옛 주소가 상세 주소에 가려지지 않음
        # 상세 화면에서 대장에 올리면 그 화면으로 돌아옴
        r = client.post(f"/files/ga.contracts/{cid}/adopt", data={"names": ["원본.pdf"], "next": f"/ga/contracts/{cid}"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/ga/contracts/{cid}?msg=") and r.headers["location"].endswith("#files")
        r = client.post(f"/files/ga.contracts/{cid}/adopt", data={"names": ["x"], "next": "https://evil.example/"}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/files/ga.contracts/{cid}")                  # 바깥 주소로는 돌아가지 않음
        r = client.post(f"/files/ga.contracts/{cid}/adopt", data={"names": ["x"], "next": "//evil.example/ga/"}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/files/ga.contracts/{cid}")
        assert archive.pending(["ga.contracts"]) and {(p["menu"], p["ref_id"]): p["n"] for p in archive.pending(["ga.contracts"])}[("ga.contracts", cid)] == 1
        # 한꺼번에 올리기
        r = client.post("/files/adopt-all", data={"items": [f"ga.contracts:{cid}", f"ga.company:{iid}", f"ga.vehicles:{vid}", "hr.employees:1", "x:y"],
                                                  "next": "/ga/contracts"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/ga/contracts?msg=")
        assert len(archive.items("ga.contracts", cid)) == 2 and len(archive.items("ga.company", iid)) == 1 and len(archive.items("ga.vehicles", vid)) == 1
        assert not [p for p in archive.pending(["ga.contracts", "ga.company", "ga.vehicles"]) if p["ref_id"] in (cid, iid, vid)]
        assert os.path.exists(os.path.join(cbase, "변경계약", "1차.pdf"))                         # 파일은 그 자리 그대로
        hist = client.get(f"/ga/contracts/{cid}").text
        assert "폴더 파일을 대장에 올림" in hist and "폴더에 있는데 대장에 없는 파일" not in hist
        # 조회 전용 계정: 상세는 보이지만 서류 묶음·고치기 버튼은 없음
        with TestClient(app) as v:
            _login(v, "co_view")
            vt = v.get(f"/ga/contracts/{cid}")
            assert vt.status_code == 200 and "지금 상태" in vt.text and "원본.pdf" not in vt.text and "내용 고치기" not in vt.text
            assert v.post("/files/adopt-all", data={"items": [f"ga.contracts:{cid}"]}, follow_redirects=False).status_code in (303, 403)
        # 저장 뒤 상세로 돌아옴
        r = client.post("/ga/contracts/save", data={"id": cid, "category": "임대차", "title": "알림시험 계약", "counterparty": "임대인B", "status": "유효",
                                                    "next": f"/ga/contracts/{cid}"}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/ga/contracts/{cid}?msg=")
        r = client.post("/ga/company/save", data={"id": iid, "category": items_mod.categories()[0], "name": "알림시험 인증", "expire_date": "2031-06-30",
                                                  "status": "유효", "next": f"/ga/contracts/item/{iid}"}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/ga/contracts/item/{iid}?msg=")
        r = client.post("/ga/company/save", data={"id": iid, "category": items_mod.categories()[0], "name": "알림시험 인증", "status": "유효",
                                                  "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"].startswith("/ga/contracts?msg=")
    finally:
        archive.pending_clear()
        execute("DELETE FROM attachments WHERE (menu='ga.contracts' AND ref_id=?) OR (menu='ga.company' AND ref_id=?) OR (menu='ga.vehicles' AND ref_id=?)", (cid, iid, vid))
        execute("DELETE FROM ga_contracts WHERE id=?", (cid,))
        execute("DELETE FROM ga_company_items WHERE id=?", (iid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


def test_new_file_notice_on_every_menu(client, two):
    """인사기록카드·자산 상세의 서류 묶음, 상벌·과태료(한 폴더를 여러 건이 같이 씀)·자산·임직원 목록의 새 파일 알림."""
    from app.modules.ga import repo as ga
    eid = _emp(two, "서류사원", "1")
    aid = ga.save("ga_assets", None, {"asset_no": "A-NEW-1", "name": "알림노트북", "category": "IT기기", "status": "사용중", "company": "1"})
    vid = ga.save("ga_vehicles", None, {"plate_no": "88아8888", "model": "과태료차", "status": "운행", "company": "1"})
    f1 = execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount, status) VALUES (?,?,?,?,?)", (vid, "2026-10-01 10:00", "속도위반", 40000, "접수"))
    f2 = execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount, status) VALUES (?,?,?,?,?)", (vid, "2026-11-01 10:00", "주정차", 40000, "접수"))
    try:
        ebase = os.path.join(archive.root(), archive.ensure_folder("hr.employees", eid))
        abase = os.path.join(archive.root(), archive.ensure_folder("ga.assets", aid))
        fbase = os.path.join(archive.root(), archive.folder("ga.fines", archive.target("ga.fines", f1)))
        os.makedirs(fbase, exist_ok=True)
        for path in (os.path.join(ebase, "주민등록등본.pdf"), os.path.join(abase, "영수증.pdf"), os.path.join(fbase, "고지서.pdf")):
            with open(path, "wb") as f:
                f.write(PDF)
        archive.pending_clear()
        # 임직원: 목록 알림 → 인사기록카드 서류 탭(보관함 한 곳)
        page = client.get("/hr/employees?co=all").text
        assert "pendingCard" in page and f"/hr/employees/{eid}?tab=files#files" in page and f'value="hr.employees:{eid}"' in page
        card = client.get(f"/hr/employees/{eid}?tab=files").text
        assert "서류 올리기" in card and "주민등록등본.pdf" in card and "폴더에 있는데 대장에 없는 파일" in card and "employee_root" not in card
        assert f'name="next" value="/hr/employees/{eid}?tab=files"' in card and "필수서류 제출 현황" in card
        r = client.post(f"/files/hr.employees/{eid}/adopt", data={"names": ["주민등록등본.pdf"], "next": f"/hr/employees/{eid}?tab=files"}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/hr/employees/{eid}?tab=files&msg=") and r.headers["location"].endswith("#files")
        assert len(archive.items("hr.employees", eid)) == 1 and "pendingCard" not in client.get("/hr/employees?co=all").text.split("서류사원")[0]
        r = client.post(f"/files/hr.employees/{eid}/upload", data={"next": f"/hr/employees/{eid}?tab=files&x=1"}, files={"files": ("통장사본.pdf", PDF, "application/pdf")}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/files/hr.employees/{eid}")                      # 정해진 모양이 아닌 돌아갈 곳은 쓰지 않음
        # 자산: 목록 알림 + 상세 화면 안 서류 묶음
        assert "pendingCard" in client.get("/ga/assets?co=all").text
        ad = client.get(f"/ga/assets/{aid}").text
        assert 'id="files"' in ad and "영수증.pdf" in ad and f'name="next" value="/ga/assets/{aid}"' in ad
        # 과태료: 건마다 폴더가 따로이므로 그 건에만 알림 (자동을 끈 상태)
        pend = [p for p in archive.pending(["ga.fines"]) if f1 in p["ids"]]
        assert len(pend) == 1 and pend[0]["ids"] == [f1] and not pend[0]["shared"] and pend[0]["url"] == f"/files/ga.fines/{f1}"
        fp = client.get("/ga/vehicles?tab=fines&status=진행&co=all").text
        assert "pendingCard" in fp and f'value="ga.fines:{f1}"' in fp and f'value="ga.fines:{f2}"' not in fp
        r = client.post("/files/adopt-all", data={"items": [f"ga.fines:{f1}", f"ga.assets:{aid}"], "next": "/ga/assets"}, follow_redirects=False)
        assert r.headers["location"].startswith("/ga/assets?msg=")
        assert len(archive.items("ga.fines", f1)) == 1 and archive.items("ga.fines", f2) == [] and len(archive.items("ga.assets", aid)) == 1
        # 상벌·교육·문서수발 화면도 알림 자리가 있어도 문제없이 열림
        for url in ("/hr/rewards", "/mgmt/training?tab=courses", "/ga/docs", "/ga/vehicles?tab=damages"):
            assert client.get(url).status_code == 200, url
        # 조회 전용 계정: 알림이 보이지 않음
        with open(os.path.join(abase, "보증서.pdf"), "wb") as f:
            f.write(PDF)
        archive.pending_clear()
        with TestClient(app) as v:
            _login(v, "co_view")
            assert "pendingCard" not in v.get("/ga/assets?co=all").text and "보증서.pdf" not in v.get(f"/ga/assets/{aid}").text
    finally:
        archive.pending_clear()
        execute("DELETE FROM attachments WHERE (menu='hr.employees' AND ref_id=?) OR (menu='ga.assets' AND ref_id=?) OR (menu='ga.fines' AND ref_id IN (?,?))", (eid, aid, f1, f2))
        execute("DELETE FROM ga_vehicle_fines WHERE id IN (?,?)", (f1, f2))
        execute("DELETE FROM ga_assets WHERE id=?", (aid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


def test_salary_hidden_until_clicked(client, two):
    """임직원 현황·개인별 연봉: 금액은 가린 채로 열리고, [연봉 보기]를 누르면 접속기록이 남음."""
    from app.db import query_one
    _emp(two, "연봉가림", "1")
    page = client.get("/hr/employees?co=all").text
    assert "sal-hide" in page and 'data-sal-toggle="임직원 현황"' in page and page.count('class="num sal"') >= 2
    page = client.get("/hr/salaries?tab=people&co=all").text
    assert "sal-hide" in page and 'data-sal-toggle="개인별 연봉"' in page and "환산시급" not in page
    assert client.post("/hr/api/salary-view", json={"page": "개인별 연봉"}).json()["ok"]
    r = query_one("SELECT action, target FROM audit_logs ORDER BY id DESC LIMIT 1")
    assert r["action"] == "연봉 보기" and r["target"] == "개인별 연봉"
    with TestClient(app) as v:
        _login(v, "co_view")
        assert v.post("/hr/api/salary-view", json={}).status_code == 403 and "data-sal-toggle" not in v.get("/hr/employees").text
