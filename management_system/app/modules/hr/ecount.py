"""이카운트 ERP 사원등록 연동 (BMS → 이카운트).

BMS 가 인사등록의 첫 단계입니다. BMS 에 새로 등록된 직원·퇴사 처리된 직원을 '이카운트 반영 대기'로 모아
두 가지 방법으로 이카운트에 넣습니다.
  1) 엑셀: 이카운트 사원등록 엑셀 양식의 칸 순서대로 채운 파일을 만들어, 담당자가 이카운트에 붙여 넣기·올리기
  2) 입력 도우미: 담당자 PC 에서 실행하는 프로그램이, 담당자가 직접 로그인한 이카운트 화면의 칸을 채움 (저장은 사람이)

이카운트 Open API 에는 사원·급여 기능이 없어 서버가 직접 넣지 않습니다. 이카운트 비밀번호는 BMS 어디에도 저장하지 않습니다.
"""
import io
import json
import math
import os
import re
import zipfile
from xml.sax.saxutils import escape

from app.common import today
from app.db import audit, execute, get_settings, query, query_one, set_setting

PAY_ITEMS_DEFAULT = "기본급,연장근로수당,연구보조비,보육수당,식대,자가운전보조금"

# 계약서 조건(params) → 급여항목 이름 (급여 정보가 비어 있을 때 최신 근로계약서에서 가져올 값)
CONTRACT_PAY_KEYS = {"pay_base": "기본급", "pay_overtime": "연장근로수당", "pay_research": "연구보조비",
                     "pay_childcare": "보육수당"}

# BMS 항목: key, 화면 이름, 구역, 이카운트 칸 이름 후보(엑셀 머리글·도우미 화면 글자 찾기에 사용)
FIELDS = [
    ("emp_no", "사원번호", "기본", ["사원번호", "사원코드", "사번"]),
    ("name", "성명", "기본", ["성명", "사원명", "이름"]),
    ("rrn", "주민등록번호", "기본", ["주민등록번호", "주민번호", "주민(외국인)등록번호", "주민(외국인)번호"]),
    ("birth_date", "생년월일", "기본", ["생년월일"]),
    ("gender", "성별", "기본", ["성별"]),
    ("hire_date", "입사일자", "기본", ["입사일자", "입사일"]),
    ("resign_date", "퇴사일자", "기본", ["퇴사일자", "퇴사일"]),
    ("resign_reason", "퇴사사유", "기본", ["퇴사사유"]),
    ("department", "부서", "기본", ["부서", "부서명", "부서코드"]),
    ("rank", "직급", "기본", ["직급", "직위", "직위/직급", "직급명"]),
    ("title", "직책", "기본", ["직책"]),
    ("employment_type", "고용형태", "기본", ["고용형태", "근로형태", "고용구분", "근로자구분"]),
    ("email", "이메일", "기본", ["이메일", "E-mail", "Email", "메일"]),
    ("phone", "휴대폰", "기본", ["휴대폰", "핸드폰", "휴대전화", "휴대폰번호", "연락처"]),
    ("address", "주소", "기본", ["주소"]),
    ("bank_name", "은행", "기본", ["은행", "은행명", "급여이체은행", "이체은행"]),
    ("bank_account", "계좌번호", "기본", ["계좌번호", "급여계좌", "급여계좌번호"]),
    ("account_holder", "예금주", "기본", ["예금주", "예금주명"]),
    ("insured_monthly", "보수월액", "기타", ["보수월액", "건강보험보수월액", "고용보험보수월액", "국민연금기준소득월액",
                                         "기준소득월액"]),
    ("householder", "세대주여부", "기타", ["세대주여부", "세대주", "세대주구분"]),
    ("dependents", "부양가족수", "기타", ["부양가족수", "부양가족", "기본공제인원", "공제대상가족수"]),
    ("children_8_20", "8~20세 자녀수", "기타", ["8~20세자녀수", "8세이상20세이하자녀", "8세이상20세이하자녀수", "자녀수",
                                              "자녀세액공제"]),
]
FIELD_LABEL = {k: label for k, label, _s, _a in FIELDS}


def norm(s) -> str:
    """머리글·화면 글자 비교용: 공백·기호·(필수) 표시 제거."""
    s = re.sub(r"\((필수|선택)\)|\[(필수|선택)\]|필수", "", str(s or ""))
    return re.sub(r"[\s\*\(\)\[\]:：·/_\-~.]", "", s).lower()


def pay_items() -> list[str]:
    raw = get_settings().get("ecount_pay_items") or PAY_ITEMS_DEFAULT
    out = []
    for x in raw.split(","):
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out


def all_targets() -> list[tuple[str, str, list[str]]]:
    """엑셀 칸·도우미가 채울 수 있는 전체 항목: (key, 이름, 이카운트 칸 이름 후보)."""
    out = [(k, label, aliases) for k, label, _s, aliases in FIELDS]
    for p in pay_items():
        out.append((f"pay:{p}", p, [p] + (["시간외수당", "연장수당", "고정연장수당"] if p == "연장근로수당" else [])))
    return out


# ════════════════════════════════════════════════════════════════════════
# 급여·연말정산 정보 (이카운트 급여지급사항·기타설정에 들어갈 값)
# ════════════════════════════════════════════════════════════════════════
def get_payroll(emp_id: int) -> dict:
    row = query_one("SELECT * FROM employee_payroll WHERE employee_id=?", (emp_id,)) or {}
    try:
        items = json.loads(row.get("pay_items") or "{}")
    except ValueError:
        items = {}
    return {"householder": row.get("householder") or "", "dependents": row.get("dependents"),
            "children_8_20": row.get("children_8_20"), "insured_monthly": row.get("insured_monthly"),
            "pay_items": items, "note": row.get("note") or "", "saved": bool(row),
            "updated_at": row.get("updated_at")}


def contract_pay(emp_id: int) -> dict:
    """최신 근로계약서에 적힌 급여항목 금액 (급여 정보를 처음 채울 때 기본값)."""
    row = query_one("SELECT params FROM hr_documents WHERE employee_id=? AND doc_group='contract' "
                    "AND params IS NOT NULL ORDER BY id DESC LIMIT 1", (emp_id,))
    try:
        params = json.loads((row or {}).get("params") or "{}")
    except ValueError:
        params = {}
    return {name: int(params[k]) for k, name in CONTRACT_PAY_KEYS.items() if str(params.get(k) or "").isdigit()
            and int(params[k]) > 0}


def suggest_insured(emp_id: int) -> int | None:
    """보수월액 참고값: 기준 연봉 ÷ 12 (비과세 항목은 담당자가 빼서 고칩니다)."""
    row = query_one("SELECT annual_salary FROM salaries WHERE employee_id=? AND year<=? ORDER BY year DESC LIMIT 1",
                    (emp_id, today().year))
    return math.ceil(row["annual_salary"] / 12) if row and row.get("annual_salary") else None


def save_payroll(emp_id: int, data: dict):
    items = {k: int(v) for k, v in (data.get("pay_items") or {}).items() if str(v).isdigit() and int(v) > 0}
    execute("INSERT INTO employee_payroll(employee_id, householder, dependents, children_8_20, insured_monthly, "
            "pay_items, note) VALUES (?,?,?,?,?,?,?) ON CONFLICT(employee_id) DO UPDATE SET "
            "householder=excluded.householder, dependents=excluded.dependents, children_8_20=excluded.children_8_20, "
            "insured_monthly=excluded.insured_monthly, pay_items=excluded.pay_items, note=excluded.note, "
            "updated_at=datetime('now','localtime')",
            (emp_id, data.get("householder") or None, data.get("dependents"), data.get("children_8_20"),
             data.get("insured_monthly"), json.dumps(items, ensure_ascii=False), data.get("note") or None))
    audit("hr", "급여·연말정산 정보 저장", f"emp#{emp_id}", "")


# ════════════════════════════════════════════════════════════════════════
# 반영 대기열
# ════════════════════════════════════════════════════════════════════════
def baseline_once():
    """한 번만: 이 기능을 넣기 전에 등록된 직원은 이미 이카운트에 있다고 보고 '기존'으로 표시."""
    if get_settings().get("ecount_baseline_v1"):
        return
    for e in query("SELECT id, resign_date FROM employees"):
        execute("INSERT OR IGNORE INTO ecount_sync(employee_id, action, status, method, done_by) "
                "VALUES (?,?,?,?,?)", (e["id"], "신규", "완료", "기존", "system"))
        if e.get("resign_date"):
            execute("INSERT OR IGNORE INTO ecount_sync(employee_id, action, status, method, done_by) "
                    "VALUES (?,?,?,?,?)", (e["id"], "퇴사", "완료", "기존", "system"))
    set_setting("ecount_baseline_v1", today().isoformat())


def pending(action: str | None = None) -> list[dict]:
    """이카운트에 아직 반영하지 않은 신규 입사자·퇴사자."""
    emps = query("SELECT id, emp_no, name, department, rank, employment_type, hire_date, resign_date, "
                 "bank_name, bank_account FROM employees ORDER BY hire_date, emp_no")
    done = {(r["employee_id"], r["action"]) for r in query("SELECT employee_id, action FROM ecount_sync")}
    rrn_ids = {r["employee_id"] for r in query("SELECT employee_id FROM employee_rrn")}
    pay = {r["employee_id"]: r for r in query("SELECT employee_id, insured_monthly, pay_items FROM employee_payroll")}
    out = []
    for e in emps:
        for act in ("신규", "퇴사"):
            if action and act != action:
                continue
            if act == "퇴사" and not e.get("resign_date"):
                continue
            if (e["id"], act) in done:
                continue
            warn = []
            if act == "신규":
                if e["id"] not in rrn_ids:
                    warn.append("주민번호 없음")
                p = pay.get(e["id"])
                if not p or not p.get("insured_monthly"):
                    warn.append("보수월액 없음")
                if not p or p.get("pay_items") in (None, "", "{}"):
                    warn.append("급여항목 없음")
                if not e.get("bank_account"):
                    warn.append("계좌 없음")
            out.append({**e, "action": act, "warn": warn})
    return out


def history(limit: int = 100) -> list[dict]:
    return query("SELECT s.*, e.name, e.emp_no, e.department FROM ecount_sync s JOIN employees e ON e.id=s.employee_id "
                 "WHERE s.method<>'기존' ORDER BY s.done_at DESC, s.id DESC LIMIT ?", (limit,))


def mark(items: list[tuple[int, str]], status: str, method: str, note: str = "") -> int:
    """대기 항목을 완료(또는 제외)로 기록. items: [(employee_id, 신규|퇴사)]"""
    if status not in ("완료", "제외"):
        return 0
    n = 0
    for emp_id, act in items:
        if act not in ("신규", "퇴사") or query_one("SELECT 1 x FROM ecount_sync WHERE employee_id=? AND action=?",
                                                    (emp_id, act)):
            continue
        execute("INSERT INTO ecount_sync(employee_id, action, status, method, note, done_by) VALUES (?,?,?,?,?,?)",
                (emp_id, act, status, method, note or None, _actor()))
        n += 1
    if n:
        audit("hr", f"이카운트 반영 {status}", f"{n}건", method)
    return n


def unmark(sync_id: int):
    row = query_one("SELECT employee_id, action FROM ecount_sync WHERE id=?", (sync_id,))
    if row:
        execute("DELETE FROM ecount_sync WHERE id=?", (sync_id,))
        audit("hr", "이카운트 반영 되돌림", f"emp#{row['employee_id']}", row["action"])


def _actor() -> str:
    from app.db import current_actor
    return current_actor.get()[1]


# ════════════════════════════════════════════════════════════════════════
# 직원 한 명의 이카운트 입력값
# ════════════════════════════════════════════════════════════════════════
def values(emp_id: int, include_rrn: bool = True) -> dict:
    from app.modules.hr import repo
    e = query_one("SELECT * FROM employees WHERE id=?", (emp_id,))
    if not e:
        return {}
    p = get_payroll(emp_id)
    items = p["pay_items"] or contract_pay(emp_id)
    v = {k: e.get(k) or "" for k in ("emp_no", "name", "birth_date", "gender", "hire_date", "resign_date",
                                     "resign_reason", "department", "rank", "title", "employment_type", "email",
                                     "phone", "address", "bank_name", "bank_account")}
    v["account_holder"] = e.get("name") or "" if e.get("bank_account") else ""
    v["rrn"] = (repo.get_rrn(emp_id) or "") if include_rrn else ""
    v["insured_monthly"] = str(p["insured_monthly"] or "")
    v["householder"] = {"Y": "Y", "N": "N"}.get(p["householder"], "")
    v["dependents"] = "" if p["dependents"] is None else str(p["dependents"])
    v["children_8_20"] = "" if p["children_8_20"] is None else str(p["children_8_20"])
    for name in pay_items():
        v[f"pay:{name}"] = str(items.get(name) or "")
    return v


# ════════════════════════════════════════════════════════════════════════
# 이카운트 엑셀 양식 (머리글 칸 순서)
# ════════════════════════════════════════════════════════════════════════
class TemplateError(ValueError):
    pass


def default_columns() -> list[dict]:
    """양식을 올리기 전 기본 칸 구성."""
    keys = ["emp_no", "name", "rrn", "hire_date", "department", "rank", "title", "email", "phone", "address",
            "bank_name", "bank_account", "account_holder", "resign_date", "insured_monthly", "householder",
            "dependents", "children_8_20"]
    cols = [{"header": FIELD_LABEL[k], "field": k} for k in keys]
    cols += [{"header": p, "field": f"pay:{p}"} for p in pay_items()]
    return cols


def get_template() -> dict:
    raw = get_settings().get("ecount_template")
    if raw:
        try:
            t = json.loads(raw)
            if t.get("columns"):
                return t
        except ValueError:
            pass
    return {"filename": "", "columns": default_columns(), "default": True}


def guess_field(header: str) -> str:
    h = norm(header)
    if not h:
        return ""
    targets = all_targets()
    for key, _label, aliases in targets:            # 정확히 같은 이름
        if h in (norm(a) for a in aliases):
            return key
    for key, _label, aliases in targets:            # 머리글이 이름을 포함 (예: 사원번호(사번))
        if any(len(norm(a)) >= 2 and norm(a) in h for a in aliases):
            return key
    return ""


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """간단한 .xlsx 읽기 (첫 시트, 값만)."""
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise TemplateError("엑셀 시트를 찾지 못했습니다")
        root = ET.fromstring(z.read(sheets[0]))
    rows = []
    for r in root.iter(f"{{{ns['m']}}}row"):
        line = {}
        for c in r.findall("m:c", ns):
            ref = re.match(r"([A-Z]+)", c.get("r", "A"))
            col = 0
            for ch in ref.group(1):
                col = col * 26 + ord(ch) - 64
            t = c.get("t")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            else:
                v = c.find("m:v", ns)
                val = (v.text or "") if v is not None else ""
                if t == "s" and val.isdigit():
                    val = shared[int(val)] if int(val) < len(shared) else ""
            line[col - 1] = val
        if line:
            rows.append([line.get(i, "") for i in range(max(line) + 1)])
        if len(rows) >= 15:
            break
    return rows


def read_template(data: bytes, filename: str) -> dict:
    """이카운트에서 내려받은 빈 사원등록 양식 → 머리글 칸 목록과 자동 짝 맞춤."""
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if ext == ".xls":
            import xlrd
            sh = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
            rows = [[str(c).strip() for c in sh.row_values(r)] for r in range(min(sh.nrows, 15))]
        elif ext == ".xlsx":
            rows = _xlsx_rows(data)
        else:
            raise TemplateError("이카운트에서 내려받은 엑셀 양식(.xlsx 또는 .xls)을 올려 주세요")
    except TemplateError:
        raise
    except Exception as ex:
        raise TemplateError(f"엑셀을 읽지 못했습니다 ({ex.__class__.__name__})") from ex
    best, best_score = None, 0
    for i, r in enumerate(rows):
        score = sum(1 for c in r if guess_field(c))
        if score > best_score:
            best, best_score = i, score
    if best is None or best_score < 2:
        raise TemplateError("머리글(사원번호·성명 같은 칸 이름)이 있는 줄을 찾지 못했습니다")
    headers = [str(c).strip() for c in rows[best]]
    while headers and not headers[-1]:
        headers.pop()
    cols = [{"header": h, "field": guess_field(h)} for h in headers]
    return {"filename": filename, "header_row": best + 1, "columns": cols, "uploaded_at": today().isoformat()}


def save_template(t: dict):
    set_setting("ecount_template", json.dumps(t, ensure_ascii=False))
    audit("hr", "이카운트 양식 저장", t.get("filename") or "기본", f"{len(t.get('columns') or [])}칸")


# ════════════════════════════════════════════════════════════════════════
# 엑셀 만들기 (.xlsx, 모든 칸을 글자로 저장해 사번·주민번호 앞자리 0 유지)
# ════════════════════════════════════════════════════════════════════════
def _col_name(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml_text(v) -> str:
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(v if v is not None else ""))
    return escape(v)


def build_xlsx(header: list[str], rows: list[list[str]], sheet: str = "사원등록") -> bytes:
    def row_xml(n, vals, style=0):
        cells = "".join(f'<c r="{_col_name(i)}{n}" t="inlineStr"{f" s=\"{style}\"" if style else ""}>'
                        f'<is><t xml:space="preserve">{_xml_text(v)}</t></is></c>' for i, v in enumerate(vals))
        return f'<row r="{n}">{cells}</row>'
    body = row_xml(1, header, 1) + "".join(row_xml(i + 2, r) for i, r in enumerate(rows))
    widths = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{max(10, min(40, len(str(h)) * 2 + 4))}" customWidth="1"/>'
                     for i, h in enumerate(header))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
                 f'<cols>{widths}</cols><sheetData>{body}</sheetData></worksheet>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="1"><numFmt numFmtId="164" formatCode="@"/></numFmts>'
              '<fonts count="2"><font><sz val="10"/><name val="맑은 고딕"/></font><font><b/><sz val="10"/><name val="맑은 고딕"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFE7EEF7"/></patternFill></fill></fills>'
              '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="164" applyNumberFormat="1"/>'
              '<xf numFmtId="164" fontId="1" fillId="2" applyNumberFormat="1" applyFont="1" applyFill="1"/></cellXfs>'
              '</styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets><sheet name="{_xml_text(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        z.writestr("xl/styles.xml", styles)
    return buf.getvalue()


def export_rows(emp_ids: list[int], include_rrn: bool, date_fmt: str = "") -> tuple[list[str], list[list[str]]]:
    t = get_template()
    fmt = date_fmt or get_settings().get("ecount_date_format") or "YYYY-MM-DD"
    header = [c["header"] for c in t["columns"]]
    rows = []
    for emp_id in emp_ids:
        v = values(emp_id, include_rrn)
        if not v:
            continue
        line = []
        for c in t["columns"]:
            val = v.get(c.get("field") or "", "")
            if c.get("field") in ("hire_date", "resign_date", "birth_date") and val:
                val = format_date(val, fmt)
            line.append(val)
        rows.append(line)
    return header, rows


def format_date(iso: str, fmt: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", iso or ""):
        return iso or ""
    return {"YYYYMMDD": iso.replace("-", ""), "YYYY/MM/DD": iso.replace("-", "/")}.get(fmt, iso)


# ════════════════════════════════════════════════════════════════════════
# 입력 도우미에 넘기는 자료
# ════════════════════════════════════════════════════════════════════════
def helper_payload(emp_id: int, action: str) -> dict:
    v = values(emp_id, include_rrn=(action == "신규"))
    if not v:
        return {}
    fmt = get_settings().get("ecount_date_format") or "YYYY-MM-DD"
    keys = ([k for k, *_ in all_targets()] if action == "신규"
            else ["emp_no", "name", "resign_date", "resign_reason"])
    fields = []
    for key, label, aliases in all_targets():
        if key not in keys or not v.get(key):
            continue
        val = v[key]
        if key in ("hire_date", "resign_date", "birth_date"):
            val = format_date(val, fmt)
        fields.append({"key": key, "label": label, "aliases": aliases, "value": val})
    return {"employee_id": emp_id, "action": action, "emp_no": v["emp_no"], "name": v["name"], "fields": fields}


def helper_files_dir() -> str:
    """입력 도우미 프로그램 원본 위치 (management_system/tools/ecount_helper)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                        "tools", "ecount_helper")
