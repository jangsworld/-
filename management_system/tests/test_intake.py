"""임직원 서류 폴더 감지·초안, 주민등록번호 보호, 전자서명·메일, 등록 화면 항목 테스트."""
import re
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import mailer, storage
from app.config import settings
from app.main import app
from app.modules.hr import intake, repo, rrn, signing
from app.modules.hr.intake import Page
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
RRN = "900315-1234567"
FAMILY_RRN = "880101-2234567"

CONTRACT = """표준근로계약서(기간의 정함이 없는 경우)
1. 근로개시일 : 2020년 03월 02일 부터
2. 근 무 장 소 : ㈜샘플 영업팀
3. 업무의 내용 : 기술영업
계약연봉금액 : ₩48,000,000
2020년 03월 02일
(사업주) 사업체명 : ㈜샘플 (전화 : 02-111-2222)
소 : 서울특별시 중구 세종대로 110
(근로자) 주
소 : 경기도 수원시 팔달구 효원로 1, 101동 202호
연 락 처 : 010-1234-5678
"""
RENEWAL = """연봉계약서
② 계약연봉 총액 : 금오천만원정(\\50,000,000)
③ 연봉계약기간: 2021년 01월 01일부터 차기 연봉 조정 확정일까지
생년월일 1990.03.15 전화번호 010-1234-5678
"""
RESIDENT = f"""주민등록표 등본
세대주 홍길동
1 본인 홍길동 {RRN}
2 처 김영희 {FAMILY_RRN}
"""
DEGREE = """학 위 수 여 증 명 서
학 과 : 소프트웨어융합공학과
학 위 명 : 공학석사
학위 수여 날짜 : 2015년 2월 20일
한국공학대학교
"""


def sample_pages():
    return [Page("01. 근로계약서.pdf", CONTRACT, True), Page("연봉계약서.pdf", RENEWAL), Page("02. 등본.pdf", RESIDENT, True),
            Page("03. 학위.pdf", DEGREE, True),
            Page("전기공사기술자 수료증.jpg", "전기공사 기술자 초급 양성교육훈련 수료증", True)]


def write_hwpx(path, text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Preview/PrvText.txt", text)


def test_ocr_falls_back_to_tesseract(monkeypatch):
    import subprocess

    from PIL import Image
    seen = {}

    def fake_run(cmd, input, capture_output, timeout):
        seen.update(cmd=cmd, png=input[:4])
        return subprocess.CompletedProcess(cmd, 0, "주민등록등본".encode(), b"")

    monkeypatch.setattr(intake.shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(intake.subprocess, "run", fake_run)
    assert intake._ocr_tesseract(Image.new("RGB", (8, 8))) == "주민등록등본"
    assert seen["cmd"][-1] == "kor+eng" and seen["png"] == b"\x89PNG"
    calls = []

    def blank_first(cmd, input, capture_output, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "등본".encode() if "--psm" in cmd else b"\n", b"")

    monkeypatch.setattr(intake.subprocess, "run", blank_first)
    assert intake._ocr_tesseract(Image.new("RGB", (8, 8))) == "등본" and len(calls) == 2
    monkeypatch.setattr(intake.shutil, "which", lambda name: None)
    assert intake._ocr_tesseract(Image.new("RGB", (8, 8))) is None


def test_rrn_utils():
    assert rrn.normalize("9003151234567") == RRN
    assert rrn.normalize("901315-1234567") is None           # 13월
    assert rrn.parse("000229-3123456") == {"birth_date": "2000-02-29", "gender": "남"}
    assert rrn.parse("800101-2123456")["gender"] == "여"
    assert rrn.mask(RRN) == "900315-1******"


def test_extract_fields():
    d = intake.extract("홍길동", sample_pages(), {"company_address": "서울특별시 중구 세종대로 110"})
    f = d["fields"]
    assert d["rrn"] == RRN                                   # 세대원 번호를 고르지 않음
    assert f["birth_date"] == "1990-03-15" and f["gender"] == "남"
    assert f["phone"] == "010-1234-5678" and f["hire_date"] == "2020-03-02"
    assert f["address"].startswith("경기도 수원시")           # 사업주 주소 제외
    assert f["department"] == "영업팀" and f["employment_type"] == "정규직"
    assert f["edu_level"] == "석사" and f["edu_school"] == "한국공학대학교"
    assert f["edu_major"] == "소프트웨어융합공학과" and f["edu_grad_year"] == "2015"
    assert [(s["year"], s["amount"]) for s in d["salaries"]] == [(2020, 48000000), (2021, 50000000)]
    assert [(q["kind"], q["grade"]) for q in d["qualifications"]] == [("전기공사기술자", "초급")]
    assert "소프트웨어경력증명서" not in str(d["qualifications"])   # 학과명 '소프트웨어'에 반응하지 않음
    assert any("대조" in c for c in d["checks"])


def test_extract_does_not_mistake_rrn_for_license_or_college_for_school():
    pages = [Page("수료증.jpg", "주민등록번호 : 800101-1******\n전기공사기술자 초급 양성교육훈련", True),
             Page("학위.pdf", "학위 수여 증명서\n공학석사\n산업기술 경영대학원", True)]
    d = intake.extract("홍길동", pages)
    assert d["qualifications"][0]["license_no"] == "" and d["qualifications"][0]["grade"] == "초급"
    assert d["fields"]["birth_date"] == "1980-01-01"
    assert "edu_school" not in d["fields"] and any("학교명" in c for c in d["checks"])


def test_extract_ignores_corporate_card_and_family_only():
    pages = [Page("카드확약서.pdf", "법인카드번호 : 우리(신용)카드 1111-2222-3333-4444", False)]
    d = intake.extract("홍길동", pages)
    assert "bank_account" not in d["fields"] and d["rrn"] is None


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='intake_hr'"):
            auth.create_user("intake_hr", "인사", "hr", "Strong-Pass-123!", must_change=False)
            auth.create_user("intake_view", "조회", "viewer", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "intake_hr", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "employee_root", str(tmp_path))
    intake._seen.clear()
    return tmp_path


def test_draft_flow(client, root):
    folder = root / "강감찬"
    folder.mkdir()
    write_hwpx(folder / "등본.hwpx", RESIDENT.replace("홍길동", "강감찬"))
    write_hwpx(folder / "근로계약서.hwpx", CONTRACT + RENEWAL)

    # 복사 중일 수 있어 첫 확인에서는 추출하지 않고, 변화가 없으면 다음 확인에서 처리
    assert intake.scan_once()["pending"] is True
    assert not repo.list_drafts("확인필요")
    assert intake.scan_once()["pending"] is False
    drafts = repo.list_drafts("확인필요")
    assert len(drafts) == 1 and drafts[0]["folder_name"] == "강감찬"

    # DB 에는 암호문만, OCR 원문은 저장하지 않음
    conn = sqlite3.connect(settings.db_path)
    raw = conn.execute("SELECT data FROM employee_drafts").fetchone()[0]
    conn.close()
    assert raw.startswith("enc:") and RRN not in raw and "처 김영희" not in str(drafts[0]["data"])

    page = client.get("/hr/employees").text
    assert "초안생성 · 확인필요" in page and "강감찬" in page and RRN not in page
    did = drafts[0]["id"]
    review = client.get(f"/hr/drafts/{did}")
    assert review.status_code == 200 and "900315-1******" in review.text and RRN not in review.text

    assert client.post(f"/hr/api/drafts/{did}/rrn", json={}).json()["rrn"] == RRN   # 원본 대조용
    count = len(repo.list_employees(status=""))
    r = client.post(f"/hr/drafts/{did}", data={"name": "강감찬", "hire_date": "2020-03-02", "initial_salary": "48000000",
                                               "employment_type": "정규직"}, follow_redirects=False)
    assert r.status_code == 303 and "/hr/employees/" in r.headers["location"]
    emp_id = int(re.search(r"/hr/employees/(\d+)", r.headers["location"]).group(1))
    emp = repo.get_employee(emp_id)
    assert emp["folder_name"] == "강감찬" and emp["birth_date"] == "1990-03-15" and emp["gender"] == "남"
    assert repo.get_rrn(emp_id) == RRN and len(repo.list_employees(status="")) == count + 1
    assert [s["year"] for s in repo.employee_salaries(emp_id)] == [2021, 2020]

    # 등록 후에는 다시 초안이 되지 않음
    write_hwpx(folder / "추가서류.hwpx", "추가")
    intake.scan_once(force=True)
    assert not repo.list_drafts("확인필요")


def test_draft_exclude_and_existing_employee_link(client, root):
    (root / "공유폴더").mkdir()
    write_hwpx(root / "공유폴더" / "문서.hwpx", "내용")
    existing = repo.list_employees(status="")[0]
    (root / existing["name"]).mkdir(exist_ok=True)
    write_hwpx(root / existing["name"] / "문서.hwpx", "내용")
    intake.scan_once(force=True)
    names = [d["folder_name"] for d in repo.list_drafts("확인필요")]
    assert names == ["공유폴더"]                                  # 기존 직원 폴더는 초안을 만들지 않음
    client.post(f"/hr/drafts/{repo.list_drafts('확인필요')[0]['id']}/exclude")
    intake.scan_once(force=True)
    assert not repo.list_drafts("확인필요")


def test_draft_removed_with_folder_and_no_root_access(client, root, monkeypatch):
    folder = root / "삭제될폴더"
    folder.mkdir()
    write_hwpx(folder / "문서.hwpx", "내용")
    intake.scan_once(force=True)
    assert repo.list_drafts("확인필요")
    (folder / "문서.hwpx").unlink()
    folder.rmdir()
    intake.scan_once(force=True)
    assert not repo.list_drafts("확인필요")
    monkeypatch.setattr(settings, "employee_root", str(root / "없는경로"))
    assert intake.scan_once()["ok"] is False and intake.state["error"]
    intake.state["error"] = ""


def test_viewer_cannot_see_drafts_or_rrn(client, root):
    emp = repo.list_employees(status="")[0]
    repo.set_rrn(emp["id"], RRN)
    v = TestClient(app)
    v.post("/login", data={"username": "intake_view", "password": "Strong-Pass-123!"}, headers=ORIGIN)
    assert v.get("/hr/drafts/1", follow_redirects=False).status_code == 403
    token = re.search(r'name="csrf-token" content="([^"]+)"', v.get("/account").text).group(1)
    assert v.post(f"/hr/api/employees/{emp['id']}/rrn", json={}, headers={**ORIGIN, "x-csrf-token": token}).status_code == 403
    assert "900315" not in v.get(f"/hr/employees/{emp['id']}").text


def test_rrn_never_exposed_except_reveal(client):
    emp = repo.list_employees(status="")[1]
    r = client.post(f"/hr/employees/{emp['id']}/edit", data={
        "name": emp["name"], "hire_date": emp["hire_date"], "employment_type": emp["employment_type"], "rrn": "800101-1234567"},
        follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_employee(emp["id"])["birth_date"] == "1980-01-01"      # 주민번호로 생년월일 자동 설정
    detail = client.get(f"/hr/employees/{emp['id']}").text
    assert "800101-1******" in detail and "800101-1234567" not in detail
    assert "800101-1234567" not in client.get("/hr/employees/export.csv").text
    assert "800101-1234567" not in str(repo.get_employee(emp["id"]))
    assert client.post(f"/hr/api/employees/{emp['id']}/rrn", json={}).json()["rrn"] == "800101-1234567"
    logs = repo.recent_audit(20)
    assert any(a["action"] == "주민등록번호 열람" for a in logs)
    assert not any("800101-1234567" in str(a) for a in logs)
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT rrn FROM employee_rrn WHERE employee_id=?", (emp["id"],)).fetchone()[0].startswith("enc:")
    conn.close()
    bad = client.post(f"/hr/employees/{emp['id']}/edit", data={"name": emp["name"], "hire_date": emp["hire_date"],
                                                               "employment_type": emp["employment_type"], "rrn": "1234"},
                      follow_redirects=False)
    assert "edit" in bad.headers["location"]


def test_employee_form_new_fields(client):
    r = client.post("/hr/employees/new", data={
        "name": "을지문덕", "hire_date": "2026-01-02", "employment_type": "기타", "employment_type_etc": "고문",
        "emergency_relation": "배우자", "emergency_contact": "010-1111-2222", "bank_account": "110-123-456789",
        "edu_level": "석사", "edu_school": "한국대학교", "edu_major": "전자공학과", "edu_grad_year": "2010",
        "q0_no": "AB-123", "q0_grade": "고급", "q0_cert": "정보통신기사", "initial_salary": "60000000",
    }, follow_redirects=False)
    assert r.status_code == 303
    emp_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    e = repo.get_employee(emp_id)
    assert e["employment_type"] == "고문" and e["bank_account"] == "110123456789"
    assert e["education"] == "석사 · 한국대학교 · 전자공학과 · 2010년 졸업" and e["emergency_relation"] == "배우자"
    quals = repo.get_qualifications(emp_id)
    assert list(quals) == ["정보통신공사기술자"] and quals["정보통신공사기술자"]["license_no"] == "AB-123"
    page = client.get(f"/hr/employees/{emp_id}").text
    assert "AB-123" in page and "(배우자)" in page
    assert 'value="고문"' in client.get(f"/hr/employees/{emp_id}/edit").text
    assert "고문" in client.get("/hr/employees").text               # 조회 필터에도 표시
    assert client.get("/hr/employees/new").text.count("임원직") == 1
    # 비우고 저장하면 자격 삭제
    client.post(f"/hr/employees/{emp_id}/edit", data={"name": "을지문덕", "hire_date": "2026-01-02", "employment_type": "정규직"})
    assert repo.get_qualifications(emp_id) == {}


def test_reward_direct_input(client):
    emp = repo.list_employees(status="")[0]
    data = {"employee_id": emp["id"], "action_date": "2026-03-01", "reason": "지각"}
    client.post("/hr/rewards/save", data={**data, "kind": "징계", "category": "경위서"})
    client.post("/hr/rewards/save", data={**data, "kind": "징계", "category": "기타징계", "category_etc": "시말서"})
    client.post("/hr/rewards/save", data={**data, "kind": "포상", "category": "기타포상", "category_etc": "아이디어상"})
    cats = {r["category"]: r for r in repo.list_rewards(employee_id=emp["id"])}
    assert {"경위서", "시말서", "아이디어상"} <= set(cats)
    assert cats["경위서"]["expunge_date"] == "2026-09-01"


def test_salary_csv_export(client):
    r = client.get("/hr/salaries/export.csv?year=2026")
    assert r.status_code == 200 and "사번" in r.text


class FakeMail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, body, attachments=None):
        self.sent.append({"to": to, "subject": subject, "body": body, "attachments": attachments or []})


@pytest.fixture
def mail(monkeypatch):
    fake = FakeMail()
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", fake)
    return fake


@pytest.fixture
def fake_pdf(monkeypatch):
    monkeypatch.setattr(storage, "render_pdf", lambda content, title: b"%PDF-1.7 " + content.encode()[:40])


def _signable(client):
    emp = next(e for e in repo.list_employees(status="재직(전체)") if e.get("email") and e.get("birth_date"))
    doc_id = repo.create_document(doc_group="contract", employee_id=emp["id"], category="정규직", title="근로계약서",
                                  content="<p>계약 본문</p>", status="작성중")
    return emp, doc_id


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _token(doc_id):
    return signing.latest_request(doc_id)["token"]


def test_esign_flow(client, mail, fake_pdf, root):
    emp, doc_id = _signable(client)
    r = client.post(f"/hr/documents/{doc_id}/sign-request", follow_redirects=False)
    assert r.status_code == 303
    assert mail.sent[0]["to"] == emp["email"] and "/sign/" in mail.sent[0]["body"]
    assert repo.get_document(doc_id)["status"] == "서명대기"

    sign = TestClient(app)                                 # 로그인 없는 직원 브라우저
    url = f"/sign/{_token(doc_id)}"
    assert "본인 확인" in sign.get(url).text
    assert "계약 본문" not in sign.get(url).text            # 본인 확인 전에는 내용 비공개
    sign.post(f"{url}/verify", data={"birth": "000000"}, headers=ORIGIN)
    assert "계약 본문" not in sign.get(url).text
    birth = emp["birth_date"].replace("-", "")[2:]
    sign.post(f"{url}/verify", data={"birth": birth}, headers=ORIGIN)
    assert "계약 본문" in sign.get(url).text

    bad = sign.post(f"{url}/submit", data={"signature": "data:text/html,x", "agree": "1"}, headers=ORIGIN)
    assert "서명과 동의" in bad.text
    ok = sign.post(f"{url}/submit", data={"signature": PNG, "agree": "1"}, headers=ORIGIN)
    assert "서명이 완료" in ok.text

    d = repo.get_document(doc_id)
    assert d["status"] == "체결완료" and d["signed_date"] and "esign-block" in d["content"]
    assert d["pdf_path"].startswith(str(root)) and "BMS_근로계약서" in d["pdf_path"]
    assert len(mail.sent) == 2 and mail.sent[1]["attachments"]    # 사본 교부
    assert signing.latest_request(doc_id)["signer_ip"]

    # 서명 후 본문 변경·재서명 불가
    client.post(f"/hr/api/documents/{doc_id}", json={"content": "<p>변조</p>"})
    assert "esign-block" in repo.get_document(doc_id)["content"]
    assert "이미 서명" in sign.get(url).text


def test_esign_blocks_changed_document_and_locks_after_failures(client, mail):
    _, doc_id = _signable(client)
    client.post(f"/hr/documents/{doc_id}/sign-request")
    url = f"/sign/{_token(doc_id)}"
    d = repo.get_document(doc_id)
    emp = repo.get_employee(d["employee_id"])
    sign = TestClient(app)
    sign.post(f"{url}/verify", data={"birth": emp["birth_date"].replace("-", "")[2:]}, headers=ORIGIN)
    repo.update_document(doc_id, content="<p>바뀐 본문</p>")
    assert "문서가 변경" in sign.get(url).text
    assert "문서가 변경" in sign.post(f"{url}/submit", data={"signature": PNG, "agree": "1"}, headers=ORIGIN).text

    _, doc2 = _signable(client)
    client.post(f"/hr/documents/{doc2}/sign-request")
    url2 = f"/sign/{_token(doc2)}"
    other = TestClient(app)
    for _ in range(signing.MAX_FAILS):
        other.post(f"{url2}/verify", data={"birth": "000000"}, headers=ORIGIN)
    assert "잠겼습니다" in other.get(url2).text
    assert "유효하지 않은" in TestClient(app).get("/sign/없는토큰").text


def test_sign_link_shown_when_mail_not_configured(client):
    _, doc_id = _signable(client)
    r = client.post(f"/hr/documents/{doc_id}/sign-request", follow_redirects=False)
    assert r.status_code == 303
    assert f"/sign/{_token(doc_id)}" in client.get(f"/hr/documents/{doc_id}").text


def test_certificate_saved_to_folder_and_emailed(client, mail, fake_pdf, root):
    emp = next(e for e in repo.list_employees(status="재직(전체)") if e.get("email"))
    tpl = repo.list_templates("certificate")[0]
    r = client.post("/hr/certificates/new", data={"employee_id": emp["id"], "template_id": tpl["id"], "purpose": "제출용"},
                    follow_redirects=False)
    doc_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    d = repo.get_document(doc_id)
    assert (root / (emp.get("folder_name") or emp["name"])).is_dir() and d["pdf_path"]   # 발급 즉시 임직원 폴더에 PDF 저장
    assert storage.read_verified(d["pdf_path"], d["pdf_sha256"]).startswith(b"%PDF")
    client.post(f"/hr/documents/{doc_id}/email")
    assert mail.sent[-1]["to"] == emp["email"] and mail.sent[-1]["attachments"]
    assert repo.get_document(doc_id)["emailed_at"]


def test_migration_adds_columns(tmp_path):
    from app import db
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE hr_documents (id INTEGER PRIMARY KEY)")
    db._add_columns(conn)
    db._add_columns(conn)                                        # 두 번 실행해도 안전
    assert "folder_name" in {r[1] for r in conn.execute("PRAGMA table_info(employees)")}
    assert "emailed_at" in {r[1] for r in conn.execute("PRAGMA table_info(hr_documents)")}
    conn.close()
