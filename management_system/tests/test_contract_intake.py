"""계약서 폴더 → 계약 등록 초안 테스트. (모든 이름·회사·금액은 가짜 값)"""
import re
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import query, set_setting
from app.main import app
from app.modules.ga import contract_intake as ci
from app.modules.ga import repo
from app.modules.hr.intake import Page
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
LEASE = """부동산 임대차 계약서
임대인 (갑) 성명 : 가나부동산 주식회사 (인)
임차인 (을) 상호 : 주식회사 가상산업 대표이사 김대표
1. 보증금 : 금 일천만원정 (₩10,000,000)
2. 월 차임 : 금 일백만원정 (₩1,000,000) 부가세 별도, 매월 10일 지급
3. 임대차 기간 : 2026년 1월 1일부터 2027년 12월 31일까지
4. 계약 만료 2개월 전까지 서면 통보가 없으면 동일한 조건으로 자동 갱신된다.
"""
SERVICE = """유지보수 용역 계약서
계약번호 : SV-2026-007
발주자 : 주식회사 가상산업
수급인 : (주)다라시스템 대표 홍길순
계약기간 : 2026.03.01 ~ 2027.02.28
계약금액 : 금 이천사백만원 (₩24,000,000, VAT 포함)
"""


def test_extract_lease():
    d = ci.extract("2026 본사 임대차", [Page("임대차.pdf", LEASE)], "주식회사 가상산업")
    f = d["fields"]
    assert f["title"] == "2026 본사 임대차" and f["category"] == "임대차"
    assert f["counterparty"] == "가나부동산 주식회사"                       # 우리 회사가 아닌 쪽
    assert (f["start_date"], f["end_date"]) == ("2026-01-01", "2027-12-31")
    assert f["amount"] == 1000000 and f["pay_cycle"] == "월" and f["deposit"] == 10000000
    assert f["amount_vat"] == "부가세별도" and f["auto_renew"] == 1 and f["notice_days"] == 60
    assert not d["checks"]


def test_extract_service_contract():
    d = ci.extract("다라시스템 유지보수", [Page("계약서.pdf", SERVICE, True)], "(주)가상산업")
    f = d["fields"]
    assert f["category"] == "유지보수" and f["counterparty"].startswith("(주)다라시스템") and "홍길순" not in f["counterparty"]
    assert (f["start_date"], f["end_date"]) == ("2026-03-01", "2027-02-28")
    assert f["amount"] == 24000000 and f["pay_cycle"] == "일시" and f["amount_vat"] == "부가세포함"
    assert f["contract_no"] == "SV-2026-007" and "auto_renew" not in f
    assert any("스캔본" in c for c in d["checks"])


def test_extract_reports_missing():
    d = ci.extract("알수없는폴더", [Page("메모.pdf", "아무 내용 없음")], "")
    assert d["fields"]["category"] == "기타" and len([c for c in d["checks"] if "찾지 못했습니다" in c]) == 4


# ── 폴더 감지 → 초안 → 등록 ───────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='ct_ga'"):
            auth.create_user("ct_ga", "총무", "ga", "Strong-Pass-123!", must_change=False)
            auth.create_user("ct_view", "조회", "viewer", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "ct_ga", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "contract_root", str(tmp_path))
    ci._seen.clear()
    set_setting("company_name", "주식회사 가상산업")
    return tmp_path


def _hwpx(path, text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Preview/PrvText.txt", text)


def test_contract_draft_flow(client, root):
    folder = root / "2026 본사 임대차"
    folder.mkdir()
    _hwpx(folder / "임대차계약서.hwpx", LEASE)
    (root / "#보관").mkdir()                                                     # '#' 폴더는 무시
    _hwpx(root / "#보관" / "x.hwpx", LEASE)

    assert ci.scan_once()["pending"] is True and not ci.list_drafts("확인필요")   # 복사 중일 수 있어 한 번 기다림
    ci.scan_once()
    drafts = ci.list_drafts("확인필요")
    assert [d["folder_name"] for d in drafts] == ["2026 본사 임대차"]
    raw = query("SELECT data FROM ga_contract_drafts WHERE id=?", (drafts[0]["id"],))
    assert drafts[0]["data"]["fields"]["counterparty"] == "가나부동산 주식회사"

    page = client.get("/ga/contracts").text
    assert "계약서 등록 초안" in page and "가나부동산 주식회사" in page and "확인·등록" in page and "draftModal" in page
    did = drafts[0]["id"]
    f = client.get(f"/ga/contract-drafts/{did}/file", params={"name": "임대차계약서.hwpx"})
    assert f.status_code == 200
    assert client.get(f"/ga/contract-drafts/{did}/file", params={"name": "../../secret.txt"}).status_code == 404   # 폴더 밖 차단

    r = client.post("/ga/contract-drafts/confirm", data={
        "draft_id": did, "category": "임대차", "title": "2026 본사 임대차", "counterparty": "가나부동산 주식회사",
        "start_date": "2026-01-01", "end_date": "2027-12-31", "status": "유효", "amount": "1000000", "amount_vat": "부가세별도",
        "pay_cycle": "월", "deposit": "10000000", "auto_renew": "1", "notice_days": "60"}, follow_redirects=False)
    assert r.status_code == 303 and "등록했습니다" in unquote(r.headers["location"])
    c = next(c for c in repo.list_contracts() if c["title"] == "2026 본사 임대차")
    assert c["folder_name"] == "2026 본사 임대차" and c["deposit"] == 10000000 and c["auto_renew"] == 1
    assert ci.get_draft(did)["status"] == "등록완료" and ci.get_draft(did)["contract_id"] == c["id"]

    page = client.get("/ga/contracts").text                                      # 계약 줄에서 서류 열기
    assert f"/ga/contracts/{c['id']}/file?name=" in page
    assert client.get(f"/ga/contracts/{c['id']}/file", params={"name": "임대차계약서.hwpx"}).status_code == 200
    again = client.post("/ga/contract-drafts/confirm", data={"draft_id": did, "title": "x", "counterparty": "y"}, follow_redirects=False)
    assert "이미 처리" in unquote(again.headers["location"])                    # 두 번 등록되지 않음

    _hwpx(folder / "추가.hwpx", "부속 합의서")                                    # 등록된 폴더는 다시 초안이 되지 않음
    ci.scan_once(force=True)
    assert not ci.list_drafts("확인필요")
    repo.delete("ga_contracts", c["id"])


def test_contract_draft_delete_and_exclude(client, root):
    for n in ("복합기 렌탈", "개인 메모"):
        (root / n).mkdir()
        _hwpx(root / n / "a.hwpx", SERVICE)
    ci.scan_once(force=True)
    ds = {d["folder_name"]: d for d in ci.list_drafts("확인필요")}
    client.post(f"/ga/contract-drafts/{ds['복합기 렌탈']['id']}/delete")
    client.post(f"/ga/contract-drafts/{ds['개인 메모']['id']}/exclude")
    assert ci.get_draft(ds["복합기 렌탈"]["id"])["status"] == "삭제" and not ci.get_draft(ds["복합기 렌탈"]["id"])["data"]
    ci.scan_once(force=True)
    assert not ci.list_drafts("확인필요")                                         # 같은 서류로는 다시 안 생김
    _hwpx(root / "복합기 렌탈" / "b.hwpx", "추가 서류")
    _hwpx(root / "개인 메모" / "b.hwpx", "추가 서류")
    ci.scan_once(force=True)
    assert [d["folder_name"] for d in ci.list_drafts("확인필요")] == ["복합기 렌탈"]   # 삭제는 서류가 바뀌면 다시, 제외는 계속 제외


def test_viewer_cannot_open_contract_files_or_drafts(client, root):
    (root / "열람시험").mkdir()
    _hwpx(root / "열람시험" / "a.hwpx", LEASE)
    ci.scan_once(force=True)
    did = next(d["id"] for d in ci.list_drafts("확인필요") if d["folder_name"] == "열람시험")
    v = TestClient(app)
    v.post("/login", data={"username": "ct_view", "password": "Strong-Pass-123!"}, headers=ORIGIN)
    assert v.get(f"/ga/contract-drafts/{did}/file", params={"name": "a.hwpx"}).status_code == 403
    assert "계약서 등록 초안" not in v.get("/ga/contracts").text


def test_contract_intake_off_without_folder(client, monkeypatch):
    monkeypatch.setattr(settings, "contract_root", "")
    assert ci.scan_once()["ok"] is False
    assert "계약서 등록 초안" not in client.get("/ga/contracts").text
