"""연차 관리: 메일플러그 휴가 내역 엑셀 → 직원별 연차 현황 → 사용 촉진(근로기준법 제61조).

- 메일플러그 근태는 외부 연결(API)이 없어 관리자가 매월 '휴가 내역' 엑셀을 내려받아 올립니다.
  같은 문서번호는 한 번만 저장되고, 같은 기간 파일을 다시 올리면 그 기간에서 사라진(취소된) 문서는 지웁니다.
- 연차 종류만 보관합니다. 사유·결재자·보건휴가 등은 저장하지 않습니다(개인정보 최소화).
- 회계연도(1/1~12/31) 기준으로 발생 연차를 계산합니다(메일플러그와 같은 방식).
- 메일플러그 '직원 연차현황' 엑셀도 올릴 수 있습니다. 올리면 그 해 발생(총 연차)은 메일플러그 숫자를 그대로 쓰고,
  사용일은 휴가 내역으로 세어 메일플러그 잔여와 다른 사람을 알려 줍니다.
- BMS 는 메일플러그를 읽기만 합니다. 휴가 신청·승인은 지금처럼 메일플러그에서 합니다.
"""
import math
import os
import re
from datetime import date, timedelta

from app.common import add_months, add_years, to_date, today
from app.db import audit, batch, current_actor, execute, query, query_one
from app import company

LEAVE_TYPES = ("연차",)             # 휴가 종류 이름에 이 말이 들어간 것만 연차 사용으로 봄 (연차, 연차(반차) 등)
PARTS = {"종일": 1.0, "오전": 0.5, "오후": 0.5}
MAX_ANNUAL = 25
HEADERS = {"doc_no": "문서번호", "start": "신청기간(시작)", "end": "신청기간(종료)", "time": "신청시간",
           "days": "신청 일수", "name": "이름", "rank": "직위", "dept": "소속", "type": "휴가 종류", "status": "상태"}


class LeaveImportError(Exception):
    pass


# ── 발생 연차 (회계연도 기준) ─────────────────────────────────────────────
def entitlement(hire, year: int, resign=None) -> dict:
    """회계연도 기준 발생 연차.

    annual : 그 해 1/1 에 생기는 연차. 입사 다음 해는 입사 연도 재직일수 비례(1일 단위 올림),
             그다음 두 해는 15일, 이후 2년마다 1일씩 더해 최대 25일 (메일플러그 회계연도 기준과 같음)
    monthly: 입사 1년 미만 기간에 매월 1일씩 생기는 연차 중 그 해에 생긴 일수 (최대 11일, 입사 1년이 되는 날 소멸)
    """
    h = to_date(hire)
    out = {"annual": 0.0, "monthly": 0.0, "monthly_expires": None, "first_year": False, "note": ""}
    if not h or h.year > year:
        return out
    end = to_date(resign)
    k = year - h.year
    if k == 1:
        days_in = (date(h.year, 12, 31) - h).days + 1
        out["annual"] = float(math.ceil(round(15 * days_in / (366 if _leap(h.year) else 365), 6)))
        if h.month != 1 or h.day != 1:
            out["note"] = f"입사 연도 재직 {days_in}일 비례"
    elif k >= 2:
        out["annual"] = float(min(MAX_ANNUAL, 15 + (k - 2) // 2))
    limit = min(today(), end) if end else today()
    first_anniv = add_years(h, 1)
    accrued = [add_months(h, m) for m in range(1, 12)]
    in_year = [d for d in accrued if d.year == year and d <= limit and d < first_anniv]
    out["monthly"] = float(len(in_year))
    if in_year:
        out["monthly_expires"] = first_anniv.isoformat()
    out["first_year"] = h <= today() < first_anniv
    if end and end < date(year, 1, 1):
        out["annual"] = out["monthly"] = 0.0
    return out


def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


# ── 엑셀 읽기 ─────────────────────────────────────────────────────────────
def parse_days(time_text: str, days_text: str = "") -> float:
    """'1d (8h)' → 1, '오후 (4h)' → 0.5, '2d (16h)' → 2, '4h' → 0.5."""
    t = (time_text or "").strip()
    for text in (t, (days_text or "").strip()):
        m = re.match(r"^(\d+(?:\.\d+)?)\s*d\b", text)
        if m:
            return float(m.group(1))
    if t.startswith("반반"):
        return 0.25
    if t.startswith(("오전", "오후", "반차")):
        return 0.5
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", days_text or t)
    if m:
        return round(float(m.group(1)) / 8 * 4) / 4      # 시간만 있으면 8시간 = 1일, 0.25일 단위
    raise ValueError(f"일수를 읽을 수 없습니다: {time_text or days_text}")


def _date_cell(v) -> str:
    if isinstance(v, (int, float)) and v > 20000:           # 엑셀 날짜 숫자
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    d = to_date(str(v)[:10])
    return d.isoformat() if d else ""


def _sheet(path: str, filename: str) -> list[list]:
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == ".xls":
            import xlrd
            sh = xlrd.open_workbook(path).sheet_by_index(0)
            rows = [sh.row_values(r) for r in range(sh.nrows)]
        elif ext == ".xlsx":
            import openpyxl
            ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
            rows = [[("" if c is None else c) for c in r] for r in ws.iter_rows(values_only=True)]
        else:
            raise LeaveImportError("메일플러그에서 내려받은 엑셀 파일(.xls)을 올려 주세요")
    except LeaveImportError:
        raise
    except ImportError as ex:
        raise LeaveImportError(".xlsx 는 읽을 수 없습니다. 메일플러그에서 받은 .xls 그대로 올려 주세요") from ex
    except Exception as ex:
        raise LeaveImportError(f"엑셀을 읽지 못했습니다 ({ex.__class__.__name__})") from ex
    return rows


def _is_status(rows: list[list]) -> bool:
    """'직원 연차현황' 엑셀인지 (머리글에 총 연차·잔여일이 있음)."""
    return any({"총 연차", "잔여일"} <= {str(c).strip() for c in r} for r in rows[:6])


def read_rows(path: str, filename: str, rows: list[list] | None = None) -> list[dict]:
    """메일플러그 '휴가 내역' 엑셀(.xls/.xlsx) → 행 목록. 머리글 이름으로 칸을 찾습니다."""
    rows = rows if rows is not None else _sheet(path, filename)
    head_idx = next((i for i, r in enumerate(rows[:10]) if "문서번호" in [str(c).strip() for c in r]), None)
    if head_idx is None:
        raise LeaveImportError("메일플러그 '휴가 내역' 엑셀이 아닙니다 (문서번호 칸이 없습니다)")
    head = [str(c).strip() for c in rows[head_idx]]
    missing = [v for v in HEADERS.values() if v not in head]
    if missing:
        raise LeaveImportError("엑셀에 필요한 칸이 없습니다: " + ", ".join(missing))
    col = {k: head.index(v) for k, v in HEADERS.items()}
    out = []
    for n, r in enumerate(rows[head_idx + 1:], start=head_idx + 2):
        if not any(str(c).strip() for c in r):
            continue
        g = lambda k: str(r[col[k]]).strip() if col[k] < len(r) else ""   # noqa: E731
        row = {"line": n, "doc_no": g("doc_no"), "name": g("name"), "rank": g("rank"), "dept": g("dept"),
               "type": g("type"), "status": g("status"), "time": g("time"),
               "start": _date_cell(r[col["start"]]), "end": _date_cell(r[col["end"]])}
        try:
            row["days"] = parse_days(row["time"], g("days"))
        except ValueError as ex:
            row["error"] = str(ex)
        if not row["doc_no"] or not row["name"] or not row["start"]:
            row["error"] = "문서번호·이름·시작일이 비어 있습니다"
        row["end"] = row["end"] or row["start"]
        out.append(row)
    return out


def file_period(filename: str, rows: list[dict]) -> tuple[str, str]:
    """파일 이름(Vacation_history_2026-09-01_2026-09-30.xls)의 기간, 없으면 행의 날짜 범위."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", filename or "")
    if m:
        return m.group(1), m.group(2)
    dates = [r["start"] for r in rows if r.get("start")] + [r["end"] for r in rows if r.get("end")]
    if not dates:
        raise LeaveImportError("기간을 알 수 없습니다")
    return min(dates), max(dates)


def is_annual(leave_type: str) -> bool:
    return any(t in (leave_type or "") for t in LEAVE_TYPES)


# ── 이름 → 임직원 ─────────────────────────────────────────────────────────
def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", name or "")).strip().lower()


def match_employee(mp_name: str, mapping: dict, employees: list[dict]) -> int | None:
    """연결표 → 같은 이름이 한 명 → 괄호 뺀 이름이 한 명. 동명이인은 담당자가 연결합니다."""
    if mp_name in mapping:
        return mapping[mp_name]
    for key in (lambda e: (e["name"] or "").strip() == mp_name.strip(), lambda e: _norm(e["name"]) == _norm(mp_name)):
        hits = [e["id"] for e in employees if key(e)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def _name_map() -> dict:
    return {r["mp_name"]: r["employee_id"] for r in query("SELECT * FROM leave_name_map")}


# ── 직원 연차현황 (메일플러그가 계산한 총 연차·사용·잔여) ─────────────────
STATUS_HEADERS = {"name": "이름", "hire": "입사일자", "total": "총 연차", "used": "사용일", "remain": "잔여일"}
STATUS_OPTIONAL = {"resign": "퇴사일자", "period": "산정 기간", "first": "1년 미만 연차", "annual": "근속연차"}


def parse_amount(text) -> float:
    """'15d' → 15, '2d 4h' → 2.5, '4h' → 0.5, '0h'·'-'·빈칸 → 0 (8시간 = 1일)."""
    t = str(text if text is not None else "").strip()
    if t in ("", "-"):
        return 0.0
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*d)?\s*(?:(\d+(?:\.\d+)?)\s*h)?", t)
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"일수를 읽을 수 없습니다: {t}")
    return round(float(m.group(1) or 0) + float(m.group(2) or 0) / 8, 2)


def read_status(rows: list[list]) -> tuple[str, list[dict]]:
    """(기준일, 행 목록)."""
    base = ""
    for r in rows[:6]:
        m = re.search(r"기준일\s*[:：]\s*(\d{4}-\d{2}-\d{2})", " ".join(str(c) for c in r))
        if m:
            base = m.group(1)
    head_idx = next(i for i, r in enumerate(rows[:6]) if {"총 연차", "잔여일"} <= {str(c).strip() for c in r})
    head = [str(c).strip() for c in rows[head_idx]]
    missing = [v for v in STATUS_HEADERS.values() if v not in head]
    if missing:
        raise LeaveImportError("연차현황 엑셀에 필요한 칸이 없습니다: " + ", ".join(missing))
    if not to_date(base):
        raise LeaveImportError("연차현황 엑셀에서 기준일을 찾지 못했습니다")
    col = {k: head.index(v) for k, v in {**STATUS_HEADERS, **STATUS_OPTIONAL}.items() if v in head}
    out = []
    for n, r in enumerate(rows[head_idx + 1:], start=head_idx + 2):
        g = lambda k: str(r[col[k]]).strip() if k in col and col[k] < len(r) else ""   # noqa: E731
        if not g("name"):
            continue
        row = {"line": n, "name": g("name"), "hire": _date_cell(r[col["hire"]]), "resign": _date_cell(g("resign")),
               "from": "", "to": ""}
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", g("period"))
        if m:
            row["from"], row["to"] = m.group(1), m.group(2)
        try:
            for k in ("first", "annual", "total", "used", "remain"):
                row[k] = parse_amount(g(k))
        except ValueError as ex:
            row["error"] = str(ex)
        out.append(row)
    return base, out


def _match_status(row: dict, mapping: dict, employees: list[dict]) -> int | None:
    """이름 연결표 → 같은 이름 한 명 → 같은 이름이 여럿이면 입사일까지 같은 한 명."""
    if row["name"] in mapping:
        return mapping[row["name"]]
    for key in (lambda e: (e["name"] or "").strip() == row["name"], lambda e: _norm(e["name"]) == _norm(row["name"])):
        hits = [e for e in employees if key(e)]
        if len(hits) > 1:
            hits = [e for e in hits if (e["hire_date"] or "") == row["hire"]]
        if len(hits) == 1:
            return hits[0]["id"]
    return None


def import_status(rows: list[list], filename: str) -> dict:
    base, items = read_status(rows)
    errors = [f"{r['line']}행: {r['error']}" for r in items if r.get("error")]
    if errors:
        raise LeaveImportError("저장하지 않았습니다. " + " / ".join(errors[:5]) + (" 외" if len(errors) > 5 else ""))
    year = int(base[:4])
    mapping = _name_map()
    employees = query("SELECT id, name, hire_date FROM employees")
    stats = {"kind": "status", "file": filename, "base": base, "year": year, "saved": 0, "resigned": 0, "empty": 0, "unmatched": []}
    with batch():
        imp = execute("INSERT INTO leave_imports(filename, period_from, period_to, total, imported_by, kind) VALUES (?,?,?,?,?,'status')",
                      (filename, base, base, len(items), current_actor.get()[1]))
        for r in items:
            if r["resign"] and r["resign"] < f"{year}-01-01":
                stats["resigned"] += 1              # 그 해 전에 퇴사한 사람
                continue
            if not r["from"]:
                stats["empty"] += 1                 # 산정 기간이 아직 없는 사람 (입사 예정 등)
                continue
            emp_id = _match_status(r, mapping, employees)
            if not emp_id:
                stats["unmatched"].append(r["name"])
                continue
            execute("INSERT INTO leave_status(employee_id, year, base_date, period_from, period_to, first_days, annual_days, "
                    "total, used, remain, mp_name, import_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(employee_id, year) DO UPDATE SET base_date=excluded.base_date, period_from=excluded.period_from, "
                    "period_to=excluded.period_to, first_days=excluded.first_days, annual_days=excluded.annual_days, "
                    "total=excluded.total, used=excluded.used, remain=excluded.remain, mp_name=excluded.mp_name, "
                    "import_id=excluded.import_id, updated_at=datetime('now','localtime')",
                    (emp_id, year, base, r["from"], r["to"], r["first"], r["annual"], r["total"], r["used"], r["remain"], r["name"], imp))
            stats["saved"] += 1
        execute("UPDATE leave_imports SET added=?, changed=0, removed=0, skipped=? WHERE id=?",
                (stats["saved"], stats["resigned"] + stats["empty"] + len(stats["unmatched"]), imp))
        audit("hr", "연차현황 엑셀 올리기", filename, f"기준일 {base} · 반영 {stats['saved']}명 · 연결 안 됨 {len(stats['unmatched'])}명")
    return stats


def get_status(emp_id: int, year: int) -> dict | None:
    return query_one("SELECT * FROM leave_status WHERE employee_id=? AND year=?", (emp_id, year))


def status_base(year: int) -> str:
    r = query_one("SELECT MAX(base_date) d FROM leave_status WHERE year=?", (year,))
    return (r or {}).get("d") or ""


def import_file(path: str, filename: str) -> dict:
    sheet = _sheet(path, filename)
    if _is_status(sheet):
        return import_status(sheet, filename)
    rows = read_rows(path, filename, sheet)
    errors = [f"{r['line']}행: {r['error']}" for r in rows if r.get("error")]
    if errors:
        raise LeaveImportError("저장하지 않았습니다. " + " / ".join(errors[:5]) + (" 외" if len(errors) > 5 else ""))
    p_from, p_to = file_period(filename, rows)
    keep = [r for r in rows if is_annual(r["type"])]
    skipped = len(rows) - len(keep)
    mapping = _name_map()
    employees = query("SELECT id, name FROM employees")
    stats = {"file": filename, "period": f"{p_from} ~ {p_to}", "total": len(rows), "added": 0, "changed": 0,
             "removed": 0, "skipped": skipped, "unmatched": set()}
    with batch():
        imp = execute("INSERT INTO leave_imports(filename, period_from, period_to, total, imported_by) VALUES (?,?,?,?,?)",
                      (filename, p_from, p_to, len(rows), current_actor.get()[1]))
        seen = set()
        for r in keep:
            seen.add(r["doc_no"])
            emp_id = match_employee(r["name"], mapping, employees)
            if not emp_id:
                stats["unmatched"].add(r["name"])
            old = query_one("SELECT * FROM leave_records WHERE doc_no=?", (r["doc_no"],))
            vals = (emp_id, r["name"], r["dept"], r["rank"], r["type"], r["start"], r["end"], r["time"], r["days"],
                    r["status"], imp)
            if not old:
                execute("INSERT INTO leave_records(employee_id, mp_name, mp_dept, mp_rank, leave_type, start_date, end_date, "
                        "time_text, days, status, import_id, doc_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", vals + (r["doc_no"],))
                stats["added"] += 1
            else:
                changed = (old["start_date"], old["end_date"], old["days"], old["status"], old["leave_type"]) != \
                          (r["start"], r["end"], r["days"], r["status"], r["type"])
                execute("UPDATE leave_records SET employee_id=COALESCE(?, employee_id), mp_name=?, mp_dept=?, mp_rank=?, "
                        "leave_type=?, start_date=?, end_date=?, time_text=?, days=?, status=?, import_id=?, "
                        "updated_at=datetime('now','localtime') WHERE doc_no=?", vals + (r["doc_no"],))
                stats["changed"] += int(changed)
        # 이 기간 파일에 더 이상 없는 문서 = 메일플러그에서 취소·삭제된 휴가
        gone = query("SELECT doc_no FROM leave_records WHERE start_date<=? AND end_date>=?", (p_to, p_from))
        for g in gone:
            if g["doc_no"] not in seen:
                execute("DELETE FROM leave_records WHERE doc_no=?", (g["doc_no"],))
                stats["removed"] += 1
        execute("UPDATE leave_imports SET added=?, changed=?, removed=?, skipped=? WHERE id=?",
                (stats["added"], stats["changed"], stats["removed"], skipped, imp))
        audit("hr", "연차 엑셀 올리기", filename,
              f"추가 {stats['added']} · 변경 {stats['changed']} · 삭제 {stats['removed']} · 연결필요 {len(stats['unmatched'])}")
    stats["unmatched"] = sorted(stats["unmatched"])
    return stats


def link_name(mp_name: str, employee_id: int):
    with batch():
        execute("INSERT INTO leave_name_map(mp_name, employee_id) VALUES (?,?) "
                "ON CONFLICT(mp_name) DO UPDATE SET employee_id=excluded.employee_id", (mp_name, employee_id))
        execute("UPDATE leave_records SET employee_id=? WHERE mp_name=?", (employee_id, mp_name))
        audit("hr", "연차 이름 연결", mp_name, f"emp#{employee_id}")


def unmatched() -> list[dict]:
    return query("SELECT mp_name, MAX(mp_dept) mp_dept, MAX(mp_rank) mp_rank, COUNT(*) n, SUM(days) days "
                 "FROM leave_records WHERE employee_id IS NULL GROUP BY mp_name ORDER BY mp_name")


def covered_months(year: int) -> list[int]:
    """그 해에 엑셀로 올린 달 (1~12)."""
    months = set()
    for r in query("SELECT period_from, period_to FROM leave_imports WHERE COALESCE(kind,'')<>'status' AND period_from<=? AND period_to>=?",
                   (f"{year}-12-31", f"{year}-01-01")):
        a, b = to_date(r["period_from"]), to_date(r["period_to"])
        d = date(a.year, a.month, 1)
        while d <= b:
            if d.year == year:
                last = (add_months(d, 1) - timedelta(days=1))
                if a <= d and b >= last:          # 그 달 전체가 들어간 파일만 '올림'으로 봄
                    months.add(d.month)
            d = add_months(d, 1)
    return sorted(months)


# ── 현황 ─────────────────────────────────────────────────────────────────
def get_adjust(emp_id: int, year: int) -> dict:
    return query_one("SELECT * FROM leave_adjust WHERE employee_id=? AND year=?", (emp_id, year)) or \
        {"employee_id": emp_id, "year": year, "days": 0.0, "used_before": 0.0, "note": ""}


def save_adjust(emp_id: int, year: int, days: float, used_before: float, note: str):
    execute("INSERT INTO leave_adjust(employee_id, year, days, used_before, note) VALUES (?,?,?,?,?) "
            "ON CONFLICT(employee_id, year) DO UPDATE SET days=excluded.days, used_before=excluded.used_before, "
            "note=excluded.note", (emp_id, year, days, used_before, note))
    audit("hr", "연차 조정", f"emp#{emp_id}", f"{year}년 발생 {days:+g} · 이전 사용 {used_before:g}")


def records(emp_id: int, year: int) -> list[dict]:
    return query("SELECT * FROM leave_records WHERE employee_id=? AND start_date BETWEEN ? AND ? ORDER BY start_date",
                 (emp_id, f"{year}-01-01", f"{year}-12-31"))


def plans(emp_id: int, year: int) -> list[dict]:
    return query("SELECT * FROM leave_plans WHERE employee_id=? AND year=? ORDER BY plan_date", (emp_id, year))


def promotions(emp_id: int, year: int) -> list[dict]:
    return query("SELECT * FROM leave_promotions WHERE employee_id=? AND year=? ORDER BY id DESC", (emp_id, year))


def _used_dates(recs: list[dict]) -> set:
    out = set()
    for r in recs:
        if r["status"] != "승인":
            continue
        a, b = to_date(r["start_date"]), to_date(r["end_date"])
        while a and b and a <= b:
            out.add(a.isoformat())
            a += timedelta(days=1)
    return out


def promotion_status(emp_id: int, year: int) -> dict:
    ps = promotions(emp_id, year)
    p1 = next((p for p in ps if p["round"] == 1 and p["status"] != "취소"), None)
    p2 = next((p for p in ps if p["round"] == 2 and p["status"] != "취소"), None)
    if p2:
        return {"label": "2차 지정통보", "cls": "dark", "p1": p1, "p2": p2}
    if p1:
        if p1["status"] == "회신완료":
            return {"label": "1차 회신완료", "cls": "success", "p1": p1, "p2": None}
        if (p1["deadline"] or "") < today().isoformat():
            return {"label": "1차 기한경과", "cls": "danger", "p1": p1, "p2": None}
        return {"label": "1차 회신대기", "cls": "warning", "p1": p1, "p2": None}
    return {"label": "", "cls": "", "p1": None, "p2": None}


def summary(emp: dict, year: int) -> dict:
    ent = entitlement(emp["hire_date"], year, emp.get("resign_date"))
    adj = get_adjust(emp["id"], year)
    recs = records(emp["id"], year)
    used = sum(r["days"] for r in recs if r["status"] == "승인") + (adj["used_before"] or 0)
    granted = ent["annual"] + ent["monthly"] + (adj["days"] or 0)
    snap = get_status(emp["id"], year)
    mp = None
    if snap:        # 메일플러그 연차현황을 올린 해: 발생은 메일플러그 총 연차 그대로 (조정의 '발생 가감'은 쓰지 않음)
        granted = snap["total"]
        ent = dict(ent, annual=snap["annual_days"], monthly=snap["first_days"], note="메일플러그 연차현황 기준")
        if snap["period_from"] and snap["period_from"] < f"{year}-01-01":      # 1년 미만: 산정 기간이 전년도 입사일부터
            used += sum(r["days"] for r in query(
                "SELECT days FROM leave_records WHERE employee_id=? AND status='승인' AND start_date>=? AND start_date<?",
                (emp["id"], snap["period_from"], f"{year}-01-01")))
        mp = {"base": snap["base_date"], "total": snap["total"], "used": snap["used"], "remain": snap["remain"],
              "from": snap["period_from"], "to": snap["period_to"], "diff": round(granted - used - snap["remain"], 2)}
    pl = plans(emp["id"], year)
    used_dates = _used_dates(recs)
    t = today().isoformat()
    upcoming = sum(PARTS.get(p["part"], 1) for p in pl if p["plan_date"] >= t and p["plan_date"] not in used_dates)
    missed = [p for p in pl if p["plan_date"] < t and p["plan_date"] not in used_dates]
    remaining = round(granted - used, 2)
    return {"emp": emp, "ent": ent, "adj": adj, "mp": mp, "granted": round(granted, 2), "used": round(used, 2),
            "remaining": remaining, "upcoming": upcoming, "missed": len(missed),
            "unplanned": max(0.0, round(remaining - upcoming, 2)),
            "rate": round(used / granted * 100) if granted else None,
            "promo": promotion_status(emp["id"], year), "records": recs, "plans": pl, "used_dates": used_dates}


def overview(year: int, department: str = "") -> list[dict]:
    """그 해 재직한 직원 (그 해 이전에 퇴사한 사람 제외)."""
    emps = query("SELECT id, emp_no, name, department, rank, hire_date, resign_date, employment_type FROM employees "
                 "WHERE hire_date<=? AND (resign_date IS NULL OR resign_date='' OR resign_date>=?)" + company.sql("") +
                 " ORDER BY department, hire_date, emp_no", (f"{year}-12-31", f"{year}-01-01"))
    if department:
        emps = [e for e in emps if e["department"] == department]
    return [summary(e, year) for e in emps]


def schedule(year: int) -> dict:
    """회계연도 기준 촉진 일정 (그 해 12/31 소멸분)."""
    return {"first_from": date(year, 7, 1), "first_to": date(year, 7, 10), "second_by": date(year, 10, 31),
            "expires": date(year, 12, 31)}


def first_year_schedule(hire) -> dict | None:
    """입사 1년 미만 월차 (입사 1년이 되는 날 소멸): 9일분은 3개월 전·1개월 전, 나중 생긴 2일분은 1개월 전·10일 전."""
    h = to_date(hire)
    if not h:
        return None
    exp = add_years(h, 1)
    return {"expires": exp, "first": add_months(exp, -3), "second": add_months(exp, -1),
            "late_first": add_months(exp, -1), "late_second": exp - timedelta(days=10)}


# ── 촉진 대상 자동 선정 · 2차 날짜 제안 ─────────────────────────────────
FIXED_HOLIDAYS = ("01-01", "03-01", "05-05", "06-06", "08-15", "10-03", "10-09", "12-25")   # 양력 공휴일 (설·추석은 연말과 무관)


def first_candidates(year: int) -> list[dict]:
    """1차 대상: 그 해 잔여가 있고, 입사 1년 미만이 아니며, 아직 1차를 보내지 않은 재직자."""
    out = []
    for s in overview(year):
        e = s["emp"]
        if e.get("resign_date") or s["ent"]["first_year"] or s["remaining"] <= 0 or s["promo"]["p1"]:
            continue
        full = query_one("SELECT email, birth_date FROM employees WHERE id=?", (e["id"],))
        s["problem"] = "" if full and full.get("email") and full.get("birth_date") else "이메일·생년월일 없음"
        s["days"] = s["remaining"]
        out.append(s)
    return out


def second_candidates(year: int) -> list[dict]:
    """2차 대상: 1차 회신 기한이 지났거나 회신했는데, 아직 계획이 없는 연차가 남은 사람 (2차 미발송)."""
    t = today().isoformat()
    out = []
    for s in overview(year):
        p1, p2 = s["promo"]["p1"], s["promo"]["p2"]
        if not p1 or p2 or s["emp"].get("resign_date"):
            continue
        if p1["status"] == "회신대기" and (p1["deadline"] or "") >= t:
            continue                                      # 아직 회신 기한 안
        replied = sum(PARTS.get(p["part"], 1) for p in s["plans"] if p["source"] == "직원회신")
        days = min(s["unplanned"], max(0.0, p1["remaining"] - replied))
        days = math.ceil(days * 2) / 2                    # 지정은 반일 단위
        if days <= 0:
            continue
        s["days"] = days
        s["reason"] = "회신 없음" if p1["status"] == "회신대기" else f"일부 회신({replied:g}일)"
        out.append(s)
    for i, s in enumerate(out):
        s["suggest"] = suggest_dates(s, year, s["days"], i, len(out))
    return out


def _workdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d.strftime("%m-%d") not in FIXED_HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def suggest_dates(s: dict, year: int, days: float, idx: int = 0, n: int = 1) -> list[tuple[str, str]]:
    """11~12월 평일에 고르게 나눠 제안. 이미 쓴 날·계획한 날은 피하고, 사람마다 시작을 조금씩 어긋나게 합니다."""
    t = today()
    start = max(t + timedelta(days=7), date(year, 11, 1)) if t <= date(year, 10, 24) else t + timedelta(days=7)
    taken = set(s.get("used_dates") or ()) | {p["plan_date"] for p in s.get("plans") or ()}
    pool = [d for d in _workdays(start, date(year, 12, 31)) if d.isoformat() not in taken]
    count = math.ceil(days)
    if not pool or count <= 0:
        return []
    count = min(count, len(pool))
    step = len(pool) / count
    shift = (idx * step / max(n, 1)) % step if n > 1 else 0
    picks = sorted({pool[min(len(pool) - 1, int(i * step + shift))] for i in range(count)})
    out = [(d.isoformat(), "종일") for d in picks]
    if days != int(days) and out:
        out[-1] = (out[-1][0], "오후")
    return out


def stage_notice(year: int) -> dict | None:
    """촉진 기간이면 담당자에게 보여줄 알림 (대시보드·연차 화면)."""
    t, sch = today(), schedule(year)
    if sch["first_from"] <= t <= sch["first_to"]:
        n = len(first_candidates(year))
        if n:
            return {"round": 1, "count": n, "text": f"1차 연차 사용 촉진 기간입니다 ({sch['first_from']:%m/%d}~{sch['first_to']:%m/%d}). "
                                                      f"대상 {n}명 명단을 확인하고 보내세요."}
    if sch["first_to"] < t <= sch["second_by"]:
        n = len(second_candidates(year))
        if n:
            return {"round": 2, "count": n, "text": f"2차 사용 시기 지정 대상 {n}명이 있습니다 ({sch['second_by']:%m/%d}까지). "
                                                      "제안 날짜를 확인하고 보내세요."}
    return None
