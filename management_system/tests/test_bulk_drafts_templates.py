"""CSV 일괄등록 안정성, 등록 초안 삭제, PDF → 템플릿 가져오기 테스트. (모든 이름·회사·숫자는 가짜 값)"""
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import query, set_setting
from app.main import app
from app.modules.hr import intake, repo
from app.modules.hr import template_import as ti
from app.modules.hr.routes import import_employees_csv
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
HEAD = "사번,성명,입사일,고용형태,부서,최종학력,학교명,연락처\n"


@pytest.fixture(scope="module", autouse=True)
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='bulk_hr'"):
            auth.create_user("bulk_hr", "인사", "hr", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "bulk_hr", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


def _count(name):
    return len([e for e in repo.list_employees(status="") if e["name"] == name])


# ── CSV 일괄등록 ──────────────────────────────────────────────────────────
def test_csv_blank_employment_type_gets_default():
    msg = import_employees_csv(HEAD + ",가나다,2026-03-02,,기술팀,학사,가상대학교,\n")
    assert "신규 1명" in msg and "정규직" in msg
    e = next(e for e in repo.list_employees(status="") if e["name"] == "가나다")
    assert e["employment_type"] == "정규직" and e["department"] == "기술팀"


def test_csv_with_errors_saves_nothing():
    before = len(repo.list_employees(status=""))
    msg = import_employees_csv(HEAD + ",라마바,2026-03-02,정규직,,,,\n,사아자,2026-13-45,정규직,,,,\n,,2026-03-02,,,,,\n")
    assert "하지 않았습니다" in msg and "3행(사아자)" in msg and "4행(이름 없음)" in msg and "날짜 형식" in msg
    assert len(repo.list_employees(status="")) == before and _count("라마바") == 0     # 맞는 줄도 저장 안 함


def test_csv_reimport_updates_instead_of_duplicating_and_keeps_blanks():
    import_employees_csv(HEAD + ",차카타,2026-04-01,계약직(기간제),영업팀,석사,가상대학교,010-0000-1111\n")
    msg = import_employees_csv(HEAD + ",차카타,2026-04-01,,관리팀,,,\n")             # 사번 없이 다시 올림
    assert "수정 1명" in msg and _count("차카타") == 1
    e = next(e for e in repo.list_employees(status="") if e["name"] == "차카타")
    assert e["department"] == "관리팀"                                             # 값 있는 칸만 바뀜
    assert e["employment_type"] == "계약직(기간제)" and e["phone"] == "010-0000-1111" and e["edu_level"] == "석사"
    assert "가상대학교" in (e["education"] or "")


def test_csv_duplicate_emp_no_in_file_rejected():
    msg = import_employees_csv(HEAD + "2099001,파하가,2026-01-01,정규직,,,,\n2099001,나다라,2026-01-01,정규직,,,,\n")
    assert "겹침" in msg and _count("파하가") == 0


def test_csv_route_runs_and_reports(client):
    r = client.post("/hr/employees/import", files={"file": ("x.csv", ("﻿" + HEAD + ",마바사,2026-05-01,,,,,\n").encode("utf-8"), "text/csv")},
                    follow_redirects=False)
    assert r.status_code == 303 and "신규 1명" in unquote(r.headers["location"])


# ── 등록 초안 삭제 ────────────────────────────────────────────────────────
@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "employee_root", str(tmp_path))
    intake._seen.clear()
    return tmp_path


def _hwpx(path, text):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Preview/PrvText.txt", text)


def test_draft_delete_is_not_cancel_and_comes_back_only_when_files_change(client, root):
    folder = root / "임시폴더직원"
    folder.mkdir()
    _hwpx(folder / "서류.hwpx", "근로계약서 연락처 010-0000-2222")
    intake.scan_once(force=True)
    d = next(d for d in repo.list_drafts("확인필요") if d["folder_name"] == "임시폴더직원")

    page = client.get(f"/hr/drafts/{d['id']}").text                              # 닫기 = 초안 유지, 삭제는 따로
    assert "닫기 (초안 유지)" in page and "초안 삭제" in page and "폴더 제외" in page
    assert "/delete" in client.get("/hr/employees").text

    r = client.post(f"/hr/drafts/{d['id']}/delete", follow_redirects=False)
    assert r.status_code == 303 and "삭제했습니다" in unquote(r.headers["location"])
    gone = repo.get_draft(d["id"])
    assert gone["status"] == "삭제" and not gone["data"]                          # 읽어 둔 개인정보도 지움
    intake.scan_once(force=True)
    assert not [x for x in repo.list_drafts("확인필요") if x["folder_name"] == "임시폴더직원"]   # 같은 서류로는 안 생김

    _hwpx(folder / "추가서류.hwpx", "등본")                                       # 서류가 바뀌면 다시 생김
    intake.scan_once(force=True)
    again = repo.get_draft(d["id"])
    assert again["status"] == "확인필요" and again["data"]["files"]

    client.post(f"/hr/drafts/{d['id']}/delete")
    r = client.get(f"/hr/drafts/{d['id']}", follow_redirects=False)                # 지운 초안 링크: 오류 대신 안내
    assert r.status_code == 303 and "삭제된 초안" in unquote(r.headers["location"])

    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()
    intake.scan_once(force=True)
    assert repo.get_draft(d["id"]) is None                                       # 폴더가 없어지면 기록도 정리


# ── PDF → 템플릿 ─────────────────────────────────────────────────────────
SCANNED = """표준근로계약서(기간의 정함이 없는 경우)
㈜가상산업 (이하 "사업주"라 함)과(와) 홍길순 (이하 "근로자"라 함)은 다음
과 같이 근로계약을 체결한다.
1. 근로개시일 : 2020년 01월 02일
2. 근 무 장 소 : ㈜가상산업 사업본부
3. 업무의 내용 : 시스템 유지관리 및 영업지원
4. 소정근로시간 : 09시00분부터 18시00분까지 (휴게시간 : 12시00분~13시00분)
5. 근무일/휴일 : 매주 5일 근무, 주휴일 매주 일요일
6. 임 금
- 월급 : ₩2,500,000원
- 임금지급일 : 매월 25일(휴일의 경우는 전일 지급)
- 지급방법 : 근로자 명의 예금통장에 입금
7. 연차유급휴가
- 연차유급휴가는 근로기준법에서 정하는 바에 따라 부여함
9. 근로계약서 교부
- 사업주는 근로계약을 체결함과 동시에 본 계약서를 사본하여 근로자의
교부요구와 관계없이 근로자에게 교부함(근로기준법 제17조 이행)
2019년 12월 30일
(사업주) 사업체명 : ㈜가상산업(000-00-
대 표 자 : 김대표 (서
처 : 010-0000-3333
명 : 홍길순 (900101-1)
"""
SETTINGS = {"company_name": "주식회사 가상산업", "ceo_name": "김대표", "pay_day": "25"}


def test_convert_scanned_contract():
    r = ti.convert([ln for ln in SCANNED.splitlines() if ln], SETTINGS)
    h = r["html"]
    assert r["doc_group"] == "contract" and r["category"] == "정규직"
    assert "홍길순" not in h and "가상산업" not in h and "900101" not in h and "010-0000" not in h  # 개인·회사 값이 남지 않음
    for ph in ("{{성명}}", "{{회사명}}", "{{계약시작일}}", "{{근무장소}}", "{{담당업무}}", "{{시업시각}}", "{{종업시각}}",
               "{{휴게시간}}", "{{월급여}}원", "{{임금지급일}}", "{{작성일}}"):
        assert ph in h, ph
    assert "다음 과 같이 근로계약을 체결한다." in h                                    # 줄바꿈으로 끊긴 문장 이어 붙임
    assert "<h3>6. 임 금</h3>" in h and "<b>1. 근로개시일</b>" in h
    assert "sign-table" in h and any("서명" in n for n in r["notes"])                # 깨진 서명란 → 표준 서명표
    assert h.count("{{작성일}}") == 1 and "김대표" not in h
    assert "매주 5일 근무" in h                                                      # 회사 고정 문구는 그대로


def test_convert_fixed_term_category_and_period():
    lines = ["표준근로계약서(기간의 정함이 있는 경우)", "1. 근로계약기간 : 2026년 1월 1일부터 2026년 12월 31일까지",
             "- 연봉 : ₩36,000,000원", "※ 일반 안내 문구입니다."]
    r = ti.convert(lines, {})
    assert r["category"] == "기간제" and "{{계약시작일}} 부터 {{계약종료일}} 까지" in r["html"] and "{{연봉}}원" in r["html"]
    assert any("이름" in n for n in r["notes"])                                       # 근로자 이름을 못 찾으면 알려 줌


def test_template_import_route_creates_inactive_template(client, monkeypatch):
    set_setting("company_name", "주식회사 가상산업")
    monkeypatch.setattr(ti, "read_lines", lambda path, name: ([ln for ln in SCANNED.splitlines() if ln], True))
    r = client.post("/hr/templates/import", files={"file": ("우리회사 근로계약서.pdf", b"%PDF-1.4 fake", "application/pdf")},
                    follow_redirects=False)
    loc = unquote(r.headers["location"])
    assert r.status_code == 303 and re.search(r"/hr/templates/\d+", loc) and "미사용" in loc and "판독" in loc
    tid = int(re.search(r"/hr/templates/(\d+)", loc).group(1))
    t = repo.get_template(tid)
    assert t["is_active"] == 0 and "{{성명}}" in t["content"] and "<script" not in t["content"]
    assert client.get(f"/hr/templates/{tid}").status_code == 200

    bad = client.post("/hr/templates/import", files={"file": ("메모.txt", b"x", "text/plain")}, follow_redirects=False)
    assert "PDF 또는" in unquote(bad.headers["location"])
    monkeypatch.setattr(ti, "read_lines", lambda path, name: (_ for _ in ()).throw(ValueError("broken")))
    broken = client.post("/hr/templates/import", files={"file": ("x.pdf", b"x", "application/pdf")}, follow_redirects=False)
    assert broken.status_code == 303 and "읽지 못했습니다" in unquote(broken.headers["location"])
    repo.delete_template(tid)


def test_template_import_reads_real_text_pdf(tmp_path):
    """실제 PDF(글자층 있음) → 줄 읽기. PDF 변환 브라우저가 없으면 건너뜀."""
    from app import storage
    try:
        pdf = storage.render_pdf("<h1>표준근로계약서</h1><p>1. 근로개시일 : 2026년 3월 2일</p>", "t")
    except Exception as ex:  # 브라우저 없음
        pytest.skip(f"PDF 변환 불가: {ex.__class__.__name__}")
    path = tmp_path / "t.pdf"
    path.write_bytes(pdf)
    lines, ocr = ti.read_lines(str(path), "t.pdf")
    assert not ocr and any("근로계약서" in ln for ln in lines)
    assert "{{계약시작일}}" in ti.convert(lines, {})["html"]
