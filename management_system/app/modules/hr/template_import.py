"""회사에서 쓰던 계약서·증명서 PDF(또는 스캔 이미지) → 문서 템플릿 초안.

글자를 읽어(글자층, 없으면 OCR) 문단·제목으로 나누고, 직원·회사마다 달라지는 값(성명, 회사명, 근로개시일, 근무장소,
임금, 근로시간 등)을 {{치환자}}로 바꿉니다. 서명란은 판독이 어려워 표준 서명표로 바꿉니다.
결과는 '미사용' 템플릿으로 저장되고, 사람이 편집 화면에서 확인한 뒤 사용으로 바꿉니다.
올린 파일은 저장하지 않습니다.
"""
import html
import os
import re
from collections import Counter

from app.modules.hr import intake
from app.modules.hr.default_templates import _SIGN

MAX_UPLOAD_MB = 20
SUPPORTED = {".pdf"} | intake.IMAGE_EXT

_DATE = r"(?:19|20)\d{2}\s*[년.\-/]\s*\d{1,2}\s*[월.\-/]\s*\d{1,2}\s*일?"
_RE_DATE_ONLY = re.compile(rf"^\s*{_DATE}\s*\.?\s*$")
_RE_BLANK_DATE = re.compile(r"^\s*(?:20\s*)?년\s+월\s+일\s*$")            # 빈 양식의 '    년   월   일'
_RE_NUM_HEAD = re.compile(r"^(\d{1,2})\s*[.)]\s*(.+)$")
_RE_ARTICLE = re.compile(r"^제\s*\d+\s*조")
_RE_BULLET = re.compile(r"^[-·•○◦※▶■□☐☑✔]\s*")
_RE_SIGN_START = re.compile(r"^[(\[]?\s*(사\s*업\s*주|갑|회\s*사)\s*[)\]]?\s*(사\s*업\s*체\s*명|상\s*호|회\s*사\s*명|[:：]|$)"
                            r"|사\s*업\s*체\s*명\s*[:：]")
_RE_RRN = re.compile(r"(?<!\d)\d{6}\s*[-–]\s*[1-8]\d{0,6}\**")
_RE_PHONE = re.compile(r"(?<!\d)0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_RE_MONEY = r"[₩￦\\]?\s*\d{1,3}(?:,\d{3})+\s*원?|[₩￦\\]\s*\d{4,}\s*원?|\d{5,}\s*원"
_RE_TIME = re.compile(r"(\d{1,2})\s*(?:시|:)\s*(\d{2})?\s*분?")
_ENDS_SENTENCE = re.compile(r"(다\.|함\.?|[음임]\.|[.)\]:：]|원정|까지)$")   # '다음'처럼 '음'으로 끝나는 낱말은 문장 끝이 아님

TITLE_CATEGORY = [  # (제목에 포함된 말, 분류, 구분)
    ("재직증명", "certificate", "재직증명"), ("경력증명", "certificate", "경력증명"),
    ("연봉계약", "contract", "연봉계약"), ("단시간", "contract", "단시간"), ("일용", "contract", "일용직"),
    ("기간의 정함이 있는", "contract", "기간제"), ("기간제", "contract", "기간제"), ("계약직", "contract", "기간제"),
]


def _spaced(word: str) -> str:
    """'가상산업' → 글자 사이 띄어쓰기를 허용하는 정규식 (스캔본은 '가 상 산 업'처럼 읽히기도 함)."""
    return r"\s*".join(re.escape(ch) for ch in word if not ch.isspace())


def _company_pattern(name: str) -> str | None:
    core = re.sub(r"\(\s*주\s*\)|㈜|주식회사", "", name or "").strip()
    if len(core) < 2:
        return None
    return r"(?:\(\s*주\s*\)\s*|㈜\s*|주식회사\s*)?" + _spaced(core) + r"(?:\s*\(\s*주\s*\)|\s*주식회사)?"


def read_lines(path: str, filename: str) -> tuple[list[str], bool]:
    """파일 → 줄 목록, 스캔 판독 여부."""
    ext = os.path.splitext(filename)[1].lower()
    pages = intake._read_pdf(path, filename) if ext == ".pdf" else intake._read_image(path, filename)
    lines = []
    for p in pages:
        lines += [re.sub(r"[ \t　]+", " ", ln).strip() for ln in p.text.replace("\r", "\n").split("\n")]
    return [ln for ln in lines if ln], any(p.ocr for p in pages)


class _Sub:
    """치환 기록을 남기며 바꾸는 도우미."""

    def __init__(self):
        self.changes = Counter()

    def __call__(self, pattern, repl, text, key, flags=0, count=0):
        new, n = re.subn(pattern, repl, text, count=count, flags=flags)
        if n:
            self.changes[key] += n
        return new


def _labeled(sub: _Sub, line: str, label: str, placeholder: str) -> str:
    """'라벨 : 값' 형태에서 값(줄 끝 또는 괄호 전까지)을 치환자로."""
    return sub(rf"({label}\s*[:：]\s*)(?!\{{\{{)[^(\n]+?(\s*(?:\(|$))", rf"\g<1>{{{{{placeholder}}}}}\g<2>", line, placeholder, count=1)


def _replace(line: str, sub: _Sub, ctx: dict) -> str:
    # 개인정보 먼저: 주민번호(어디서든 생년월일만), 전화번호
    line = sub(_RE_RRN, "{{생년월일}}", line, "생년월일")
    for key, pat in ctx["company"]:
        line = sub(pat, f"{{{{{key}}}}}", line, key)
    line = sub(_RE_PHONE, "{{연락처}}", line, "연락처")
    if ctx["person"]:
        line = sub(rf"(?<![가-힣]){_spaced(ctx['person'])}(?![가-힣])", "{{성명}}", line, "성명")

    # 기간: 'A 부터 B 까지' → 계약시작일/계약종료일, 단독 근로개시일
    line = sub(rf"{_DATE}\s*(부터|~|-)\s*{_DATE}\s*(까지)?",
               lambda m: "{{계약시작일}}" + (" 부터 " if m.group(1) == "부터" else " ~ ") + "{{계약종료일}}" + (" 까지" if m.group(2) else ""),
               line, "계약기간")
    line = sub(rf"((?:근로\s*개시일|입\s*사\s*일|계약\s*시작일)\s*[:：]?\s*){_DATE}", r"\g<1>{{계약시작일}}", line, "계약시작일")
    line = sub(rf"(생\s*년\s*월\s*일\s*[:：]?\s*){_DATE}", r"\g<1>{{생년월일}}", line, "생년월일")

    # 라벨이 있는 값
    for label, ph in ((r"근\s*무\s*장\s*소", "근무장소"), (r"업\s*무\s*(?:의\s*)?내\s*용(?:\s*\(직종\))?", "담당업무"),
                      (r"수\s*습\s*기\s*간", "수습기간"), (r"부\s*서", "부서"), (r"직\s*[급위]", "직급"),
                      (r"(?<!회사)(?<!사업장)주\s*소", "주소"), (r"성\s*명", "성명")):
        line = _labeled(sub, line, label, ph)

    # 근로시간: 첫 두 시각 → 시업·종업, 휴게시간 값
    if re.search(r"근\s*로\s*시\s*간|시\s*업|근무\s*시간", line) and len(_RE_TIME.findall(line)) >= 2:
        it = iter(("{{시업시각}}", "{{종업시각}}"))
        line = _RE_TIME.sub(lambda m: next(it, m.group(0)), line, count=2)
        sub.changes["근로시간"] += 1
    line = sub(r"(휴\s*게\s*시\s*간\s*[:：]?\s*)[^)\n]+", r"\g<1>{{휴게시간}}", line, "휴게시간", count=1)

    # 임금
    for label, ph in ((r"연\s*봉(?:\s*총\s*액)?", "연봉"), (r"월\s*급(?:\s*여)?|월\s*지\s*급\s*액", "월급여"),
                      (r"시\s*급|시간\s*급", "시급"), (r"일\s*급|일\s*당", "일급")):
        line = sub(rf"((?:{label})[^0-9₩￦\\\n]{{0,12}}?)(?:{_RE_MONEY})", rf"\g<1>{{{{{ph}}}}}원", line, ph, count=1)
    line = sub(r"금\s*[일이삼사오육칠팔구십백천만억\s]{3,}원\s*정", "금 {{연봉_한글}}원정", line, "연봉_한글")
    line = sub(r"(임\s*금\s*지\s*급\s*일\s*[:：]?\s*매\s*월\s*)\d{1,2}(\s*일)", r"\g<1>{{임금지급일}}\g<2>", line, "임금지급일")
    return line


def _blocks(lines: list[str]) -> list[tuple[str, str]]:
    """줄 → (종류, 글) 블록. 줄바꿈으로 끊긴 문장은 이어 붙입니다."""
    out: list[list[str]] = []
    for ln in lines:
        m = _RE_NUM_HEAD.match(ln)
        if m:
            out.append(["head", ln])
        elif _RE_ARTICLE.match(ln):
            out.append(["head", ln])
        elif _RE_BULLET.match(ln):
            out.append(["p", ln])
        elif out and out[-1][0] == "p" and not _ENDS_SENTENCE.search(out[-1][1]):
            out[-1][1] += " " + ln
        elif out and out[-1][0] == "head" and not re.search(r"[:：]", out[-1][1]) and len(out[-1][1]) > 25 \
                and not _ENDS_SENTENCE.search(out[-1][1]):
            out[-1][1] += " " + ln                                   # 긴 조항 제목이 두 줄로 나뉜 경우
        else:
            out.append(["p", ln])
    return [(k, t) for k, t in out]


def _block_html(kind: str, text: str) -> str:
    esc = html.escape(text, quote=False)
    if kind == "head":
        m = re.match(r"^((?:\d{1,2}\s*[.)]|제\s*\d+\s*조)[^:：]*?)\s*([:：])\s*(.*)$", esc)
        if m and m.group(3):                                         # '1. 근로개시일 : 값' → 굵은 항목명 + 값
            return f"<p><b>{m.group(1).strip()}</b> : {m.group(3)}</p>"
        return f"<h3>{esc.rstrip(':：')}</h3>"
    return f"<p>{esc}</p>"


def convert(lines: list[str], settings: dict) -> dict:
    """줄 목록 → {html, doc_group, category, name, changes, notes}."""
    notes = []
    if not lines:
        return {"html": "", "doc_group": "contract", "category": "정규직", "name": "", "changes": Counter(),
                "notes": ["글자를 읽지 못했습니다"]}
    head = lines[:6]
    title_idx = next((i for i, ln in enumerate(head) if re.search(r"계\s*약\s*서|증\s*명\s*서|서\s*약\s*서", ln)), 0)
    title = lines[title_idx]
    norm_title = re.sub(r"\s", "", title)
    doc_group, category = "contract", "정규직"
    for word, g, c in TITLE_CATEGORY:
        if re.sub(r"\s", "", word) in norm_title:
            doc_group, category = g, c
            break

    text = "\n".join(lines)
    person = None
    for pat in (r"과\s*\(\s*와\s*\)\s*([가-힣]{2,4})\s*\(\s*이하", r"성\s*명\s*[:：]\s*([가-힣]{2,4})(?![가-힣])",
                r"근로자\s*\)?\s*[:：]?\s*([가-힣]{2,4})\s*\(\s*이하"):
        m = re.search(pat, text)
        if m and m.group(1) not in ("이하", "근로자", "사업주", "갑", "을"):
            person = m.group(1)
            break
    company = []
    for key, skey in (("회사명", "company_name"), ("대표자", "ceo_name"), ("사업자번호", "business_no"),
                      ("회사전화", "company_phone"), ("회사주소", "company_address")):
        val = (settings.get(skey) or "").strip()
        if not val:
            continue
        if key == "회사명":
            pat = _company_pattern(val)
        elif key in ("사업자번호", "회사전화"):
            digits = re.sub(r"\D", "", val)
            pat = r"[-.\s]?".join(digits) if len(digits) >= 7 else None
        else:
            pat = _spaced(val) if len(val) >= 2 else None
        if pat:
            company.append((key, pat))
    ctx = {"person": person, "company": company}

    # 서명란: 뒤쪽 절반에서 '(사업주) 사업체명 …' 이 시작되는 곳부터는 표준 서명표로 바꿈
    body, sign_date, sign_found = lines[title_idx + 1:], False, False
    half = len(body) // 2
    sign_idx = next((i for i in range(half, len(body)) if _RE_SIGN_START.search(body[i])), None)
    if sign_idx is not None:
        sign_found = True
        date_idx = next((i for i in range(sign_idx - 1, max(sign_idx - 4, -1), -1)
                         if _RE_DATE_ONLY.match(body[i]) or _RE_BLANK_DATE.match(body[i])), None)
        sign_date = date_idx is not None
        body = body[:date_idx if date_idx is not None else sign_idx]

    sub = _Sub()
    parts = [f'<h1 class="doc-title">{html.escape(re.sub(r"[{}]", "", title), quote=False)}</h1>']
    for kind, t in _blocks(body):
        if not sign_found and (_RE_DATE_ONLY.match(t) or _RE_BLANK_DATE.match(t)):   # 서명란이 없을 때 단독 날짜 = 작성일
            parts.append('<p style="text-align:center;margin-top:28px">{{작성일}}</p>')
            sub.changes["작성일"] += 1
            continue
        parts.append(_block_html(kind, _replace(t, sub, ctx)))
    if sign_found:
        if doc_group == "contract":                                   # 표준 서명표에 작성일 줄이 들어 있음
            parts.append(_SIGN.strip())
            sub.changes["작성일"] += 1
            notes.append("서명란은 표준 서명표(사업주·근로자)로 바꿨습니다")
        elif sign_date:
            parts.append('<p style="text-align:center;margin-top:28px">{{작성일}}</p>')
            sub.changes["작성일"] += 1
    html_out = "\n".join(p for p in parts if p)

    left_dates = len(re.findall(_DATE, re.sub(r"\{\{[^}]+\}\}", "", html_out)))
    left_money = len(re.findall(_RE_MONEY, html_out))
    if left_dates or left_money:
        notes.append(f"그대로 둔 날짜 {left_dates}곳·금액 {left_money}곳이 있습니다. 직원마다 다르면 치환자로 바꾸세요")
    if not person:
        notes.append("근로자 이름을 찾지 못했습니다. 이름이 남아 있으면 {{성명}}으로 바꾸세요")
    return {"html": html_out, "doc_group": doc_group, "category": category, "name": re.sub(r"\s+", " ", title)[:60],
            "changes": sub.changes, "notes": notes}


def summary(result: dict, ocr: bool) -> str:
    ch = result["changes"]
    done = ", ".join(f"{k} {v}" for k, v in ch.most_common()) or "없음"
    msg = f"PDF에서 템플릿을 만들었습니다 (미사용 상태). 바꾼 곳: {done}."
    if ocr:
        msg += " 스캔본이라 글자를 판독했습니다. 오타가 있는지 확인하세요."
    if result["notes"]:
        msg += " " + " ".join(n + "." for n in result["notes"])
    return msg + " 확인 후 [사용]을 체크하고 저장하세요"
