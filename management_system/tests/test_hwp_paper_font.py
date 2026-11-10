"""한글(.hwpx/.hwp) → 템플릿, 기존 서면계약 체결 처리, 문서 글꼴 설정 테스트. (가짜 값만 사용)"""
import io
import os
import re
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.common import doc_font_css
from app.db import get_settings, set_setting
from app.main import app
from app.modules.hr import hwp_reader, repo
from app.modules.hr import template_import as ti
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"'
HEADER = f"""<hh:head {NS}><hh:refList>
<hh:charProperties><hh:charPr id="0" height="1000"/><hh:charPr id="1" height="1600"><hh:bold/></hh:charPr><hh:charPr id="2" height="1000"><hh:bold/></hh:charPr></hh:charProperties>
<hh:paraProperties><hh:paraPr id="0"><hh:align horizontal="JUSTIFY"/></hh:paraPr><hh:paraPr id="1"><hh:align horizontal="CENTER"/></hh:paraPr></hh:paraProperties>
</hh:refList></hh:head>"""


def _p(text, para="0", char="0"):
    return f'<hp:p paraPrIDRef="{para}"><hp:run charPrIDRef="{char}"><hp:t>{text}</hp:t></hp:run></hp:p>'


def _form(title):
    return (_p(title, "1", "1")
            + _p("                (이하 “사업주”라 함)과(와)            (이하 “근로자”라 함)은 다음과 같이 근로계약을 체결한다.")
            + f'<hp:p paraPrIDRef="0"><hp:run charPrIDRef="2"><hp:t>1. 근로계약기간 :</hp:t></hp:run><hp:run charPrIDRef="0"><hp:t>      년   월   일부터      년   월   일까지</hp:t></hp:run></hp:p>'
            + _p("2. 근 무 장 소 : ")
            + _p("4. 소정근로시간 :   시  분 ~   시  분 (휴게 :  시  분 ~  시  분) (1일  시간, 1주  시간)")
            + _p("  - 월(일, 시간)급 :                    원")
            + _p("  - 임금지급일 : 매월(매주 또는 매일)       일(휴일의 경우는 전날 지급)")
            + _p("      년      월      일", "1")
            + '<hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:tbl><hp:tr>'
              '<hp:tc><hp:cellSpan colSpan="1" rowSpan="2"/><hp:subList>' + _p("(사업주)") + '</hp:subList></hp:tc>'
              '<hp:tc><hp:cellSpan colSpan="2" rowSpan="1"/><hp:subList>' + _p("사업체명 :            (전화 :        )") + '</hp:subList></hp:tc></hp:tr>'
              '<hp:tr><hp:tc><hp:subList>' + _p("대 표 자 :           (서명)") + '</hp:subList></hp:tc>'
              '<hp:tc><hp:subList>' + _p("주    소 :") + '</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>'
            + _p("(근로자) 주    소 :") + _p("성    명 :                   (서명)"))


def make_hwpx(forms=("표준근로계약서(기간의 정함이 있는 경우)",)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/header.xml", HEADER)
        z.writestr("Contents/section0.xml", f"<hs:sec {NS} xmlns:hs=\"http://www.hancom.co.kr/hwpml/2011/section\">"
                   + "".join(_form(t) for t in forms) + "</hs:sec>")
    return buf.getvalue()


def test_hwpx_reader_keeps_layout(tmp_path):
    path = tmp_path / "a.hwpx"
    path.write_bytes(make_hwpx())
    blocks = hwp_reader.read(str(path), "a.hwpx")
    title = blocks[0]
    assert title["align"] == "center" and title["size"] == 16 and title["runs"][0][1] is True
    table = next(b for b in blocks if b["t"] == "table")
    assert table["rows"][0][0]["rowspan"] == 2 and table["rows"][0][1]["colspan"] == 2


def test_convert_hwp_fills_blanks(tmp_path):
    path = tmp_path / "a.hwpx"
    path.write_bytes(make_hwpx())
    [r] = ti.convert_hwp(hwp_reader.read(str(path), "a.hwpx"), {"company_name": "주식회사 가상산업"})
    h = r["html"]
    assert r["category"] == "기간제" and h.startswith('<h1 class="doc-title">표준근로계약서(기간의 정함이 있는 경우)</h1>')
    for ph in ("{{회사명}}(이하", "과(와) {{성명}}", "{{계약시작일}}부터 {{계약종료일}}까지", "근 무 장 소 : {{근무장소}}",
               "{{시업시각}} ~ {{종업시각}}", "(휴게 : {{휴게시간}})", "1주 {{주소정근로시간}}시간", "{{월급여}}원",
               "{{임금지급일}}일", "{{작성일}}", "사업체명 : {{회사명}}", "(전화 : {{회사전화}}", "대 표 자 : {{대표자}}",
               "{{회사주소}}", "소 : {{주소}}", "명 : {{성명}} (서명)"):
        assert ph in h, ph
    assert re.search(r"\(근로자\) 주[ &nbsp;;]+소 : \{\{주소\}\}", h)                 # 근로자 쪽 주소는 직원 주소
    assert "<b>1. 근로계약기간 :</b>" in h                                     # 굵은 항목명 유지
    assert 'rowspan="2"' in h and 'colspan="2"' in h                          # 표 병합 유지
    assert h.count("{{") == h.count("}}") and "{{{{" not in h and "}}}}" not in h


def test_convert_hwp_splits_multiple_forms(tmp_path):
    path = tmp_path / "b.hwpx"
    path.write_bytes(make_hwpx(("표준근로계약서(기간의 정함이 없는 경우)", "단시간근로자 표준근로계약서", "건설일용근로자 표준근로계약서")))
    res = ti.convert_hwp(hwp_reader.read(str(path), "b.hwpx"), {})
    assert [r["category"] for r in res] == ["정규직", "단시간", "일용직"] and len({r["name"] for r in res}) == 3


def test_hwp_errors(tmp_path):
    bad = tmp_path / "x.hwp"
    bad.write_bytes(b"not an hwp")
    with pytest.raises(hwp_reader.HwpError):
        hwp_reader.read(str(bad), "x.hwp")
    with pytest.raises(hwp_reader.HwpError):
        hwp_reader.read(str(bad), "x.hwpx")


STD_HWP = r"C:\BMS_local\std_contract.hwp"   # 고용노동부 표준근로계약서(개인정보 없음). 있는 PC 에서만 시험


@pytest.mark.skipif(not os.path.exists(STD_HWP), reason="표준근로계약서 한글 파일 없음")
def test_real_hwp_binary_standard_form():
    res = ti.convert_hwp(hwp_reader.read(STD_HWP, "std.hwp"), {})
    assert len(res) >= 5 and res[0]["category"] == "정규직" and res[1]["category"] == "기간제"
    h = res[0]["html"]
    assert "{{근무장소}}" in h and "{{회사주소}}" in h and "{{작성일}}" in h and "{{{{" not in h


# ── 화면 ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        if not auth.query_one("SELECT 1 FROM users WHERE username='hp_admin'"):
            auth.create_user("hp_admin", "관리", "admin", "Strong-Pass-123!", must_change=False)
        c.post("/login", data={"username": "hp_admin", "password": "Strong-Pass-123!"}, headers=ORIGIN)
        c.headers.update(ORIGIN)
        c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
        yield c


def test_template_import_hwpx_route(client):
    before = {t["id"] for t in repo.list_templates()}
    one = client.post("/hr/templates/import", files={"file": ("양식.hwpx", make_hwpx(), "application/octet-stream")}, follow_redirects=False)
    assert re.search(r"/hr/templates/\d+", one.headers["location"]) and "한글 문서" in unquote(one.headers["location"])
    many = client.post("/hr/templates/import", files={"file": ("묶음.hwpx", make_hwpx(("정규직 근로계약서", "단시간근로자 근로계약서")), "application/octet-stream")},
                       follow_redirects=False)
    loc = unquote(many.headers["location"])
    assert loc.startswith("/hr/templates?") and "양식 2개" in loc
    new = [t for t in repo.list_templates() if t["id"] not in before]
    assert len(new) == 3 and all(t["is_active"] == 0 for t in new)
    bad = client.post("/hr/templates/import", files={"file": ("x.hwp", b"broken", "application/octet-stream")}, follow_redirects=False)
    assert "손상" in unquote(bad.headers["location"])
    for t in new:
        repo.delete_template(t["id"])


def test_template_editor_returns_to_list(client):
    page = client.get("/hr/templates/0").text
    assert "location.href = '/hr/templates?msg='" in page and 'id="fontSel"' in page


def test_doc_font_setting(client):
    assert "Malgun Gothic" in doc_font_css("맑은 고딕") and "Batang" in doc_font_css("바탕") and "Malgun" in doc_font_css("없는글꼴")
    client.post("/settings", data={"doc_font": "바탕"})
    assert get_settings()["doc_font"] == "바탕" and ".paper{font-family:'Batang'" in client.get("/hr/templates/0").text
    from app import storage
    assert "Batang" in storage._paper_css().rsplit(".paper{font-family", 1)[1]
    set_setting("doc_font", "맑은 고딕")
    assert ".paper{font-family:'Malgun Gothic'" in client.get("/hr/templates").text


def test_doc_font_css_works_without_master_key(monkeypatch):
    """서버 점검(bms-selftest)처럼 암호화 키 없이 PDF 를 만들 때도 글꼴 CSS 가 실패하지 않아야 함."""
    from app.security import crypto
    set_setting("mail_password_test_only", crypto.enc("secret"))
    monkeypatch.setattr(crypto, "_key", None, raising=False)
    monkeypatch.setattr(crypto, "dec", lambda v: (_ for _ in ()).throw(RuntimeError("암호화 키가 로드되지 않았습니다")), raising=False)
    assert "Malgun Gothic" in doc_font_css()


def test_paper_contract_bulk_single_and_new(client):
    ids = []
    for n in ("서면일", "서면이"):
        ids.append(repo.create_employee({"name": n, "hire_date": "2020-03-02", "employment_type": "정규직"}))
    assert not set(ids) & repo.valid_contract_ids()
    page = client.get("/hr/contracts").text
    assert "/hr/contracts/paper" in page and "기존 서면계약 체결" in page and "data-check-all" in page
    r = client.post("/hr/contracts/paper", data={"employee_ids": [str(i) for i in ids]}, follow_redirects=False)
    assert "2명" in unquote(r.headers["location"]) and set(ids) <= repo.valid_contract_ids()
    doc = [d for d in repo.list_documents("contract", employee_id=ids[0])][0]
    assert doc["status"] == "체결완료" and doc["start_date"] == "2020-03-02" and "기존 서면" in doc["title"]
    again = client.post("/hr/contracts/paper", data={"employee_ids": [str(ids[0])]}, follow_redirects=False)
    assert "0명" in unquote(again.headers["location"])                               # 두 번 만들지 않음

    one = repo.create_employee({"name": "서면삼", "hire_date": "2025-01-01", "employment_type": "계약직(기간제)"})
    assert "기존 서면계약 체결 처리" in client.get(f"/hr/employees/{one}?tab=contract").text
    client.post(f"/hr/employees/{one}/paper-contract", data={"end_date": "2026-12-31"})
    d = repo.list_documents("contract", employee_id=one)[0]
    assert d["end_date"] == "2026-12-31" and d["category"] == "기간제"

    assert 'name="paper_contract"' in client.get("/hr/employees/new").text
    r = client.post("/hr/employees/new", data={"name": "서면사", "hire_date": "2019-05-01", "employment_type": "정규직",
                                               "paper_contract": "1"}, follow_redirects=False)
    new_id = int(re.search(r"/hr/employees/(\d+)", r.headers["location"]).group(1))
    assert new_id in repo.valid_contract_ids() and "기존 서면계약" in unquote(r.headers["location"])
    for i in ids + [one, new_id]:
        repo.delete_employee(i)
