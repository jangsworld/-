"""인사발령: 직급·직책·부서·근무지·고용형태 변경을 한곳에서 등록·조회.

- 적용: 발령일이 오늘 이전이면 인사·고용 정보도 함께 바꿉니다.
- 예정: 발령일이 미래면 '예정'으로 두었다가 그날 자동 적용합니다.
- 이력만: 지난 조직개편 기록을 채울 때. 지금의 인사·고용 정보는 건드리지 않습니다.
"""
import html
import logging
import threading

from app.common import to_date, today
from app.db import audit, batch, dept_scope, execute, get_codes, get_settings, query, query_one

log = logging.getLogger("bms.appoint")

# 칸 → (화면 이름, 발령 구분, 구분값 그룹)
FIELDS = {
    "department": ("부서", "전보", "DEPT"),
    "rank": ("직급", "승진", "RANK"),
    "title": ("직책", "직책변경", "TITLE"),
    "work_location": ("근무지", "근무지변경", "LOCATION"),
    "employment_type": ("고용형태", "고용형태변경", "EMPLOYMENT_TYPE"),
}
TYPE_FIELD = {t: f for f, (_n, t, _g) in FIELDS.items()}
MODES = ("apply", "history")


class AppointError(ValueError):
    pass


def field_of(row: dict) -> str:
    return row.get("field") or TYPE_FIELD.get(row.get("appt_type") or "", "")


def rows(date_from: str = "", date_to: str = "", department: str = "", appt_type: str = "", batch_title: str = "",
         q: str = "", limit: int = 1000) -> list[dict]:
    sql = ("SELECT a.*, e.name, e.emp_no, e.department, e.rank FROM appointments a JOIN employees e ON e.id=a.employee_id WHERE 1=1")
    p: list = []
    if date_from:
        sql += " AND a.appt_date >= ?"
        p.append(date_from)
    if date_to:
        sql += " AND a.appt_date <= ?"
        p.append(date_to)
    if appt_type:
        sql += " AND a.appt_type = ?"
        p.append(appt_type)
    if batch_title:
        sql += " AND a.batch_title = ?"
        p.append(batch_title)
    out = query(sql + " ORDER BY a.appt_date DESC, a.id DESC LIMIT ?", p + [int(limit)])
    if department:
        scope = dept_scope(department)
        out = [r for r in out if r.get("department") in scope or r.get("from_value") in scope or r.get("to_value") in scope]
    if q:
        out = [r for r in out if q in (r.get("name") or "") or q in (r.get("batch_title") or "") or q in (r.get("note") or "")]
    for r in out:
        r["planned"] = r.get("status") == "예정"
        r["field_name"] = FIELDS[field_of(r)][0] if field_of(r) in FIELDS else ""
    return out


def batches(limit: int = 30) -> list[dict]:
    return query("SELECT batch_title, MIN(appt_date) d1, MAX(appt_date) d2, COUNT(*) n, COUNT(DISTINCT employee_id) people, "
                 "SUM(CASE WHEN status='예정' THEN 1 ELSE 0 END) planned FROM appointments "
                 "WHERE batch_title IS NOT NULL AND batch_title<>'' GROUP BY batch_title ORDER BY d2 DESC LIMIT ?", (limit,))


def movement(rs: list[dict]) -> list[dict]:
    """부서 이동 현황: 부서별로 나간 인원·들어온 인원·증감 (부서 발령만)."""
    stat: dict = {}
    for r in rs:
        if field_of(r) != "department":
            continue
        for key, col in (("out", "from_value"), ("in", "to_value")):
            if r.get(col):
                stat.setdefault(r[col], {"out": 0, "in": 0})[key] += 1
    return sorted(({"department": d, **v, "net": v["in"] - v["out"]} for d, v in stat.items()), key=lambda x: (-abs(x["net"]), x["department"]))


def _apply(conn_execute, emp_id: int, field: str, value: str):
    conn_execute(f"UPDATE employees SET {field}=?, updated_at=datetime('now','localtime') WHERE id=?", (value, emp_id))


def register(emp_ids: list[int], appt_date: str, changes: dict, title: str = "", note: str = "", mode: str = "apply",
             from_values: dict | None = None, by: str = "") -> dict:
    """발령 등록. changes: {칸: 새 값}. mode='history' 는 이력만(소급), 이때 from_values 로 변경 전 값을 줄 수 있습니다."""
    d = to_date(appt_date)
    if not d:
        raise AppointError("발령일을 넣어 주세요")
    if mode not in MODES:
        raise AppointError("처리 방식을 고르세요")
    changes = {f: (v or "").strip() for f, v in changes.items() if f in FIELDS and (v or "").strip()}
    if not changes:
        raise AppointError("바꿀 항목(부서·직급·직책·근무지·고용형태)을 하나 이상 고르세요")
    for f, v in changes.items():
        if v not in get_codes(FIELDS[f][2]):
            raise AppointError(f"{FIELDS[f][0]} '{v}'은(는) 구분값 설정에 없는 값입니다")
    emp_ids = list(dict.fromkeys(int(i) for i in emp_ids))
    if not emp_ids:
        raise AppointError("직원을 한 명 이상 고르세요")
    if mode == "history" and d > today():
        raise AppointError("'이력만 추가'는 지난 날짜에만 쓸 수 있습니다")
    planned = mode == "apply" and d > today()
    title, note = (title or "").strip()[:100] or None, (note or "").strip()[:300] or None
    added = skipped = 0
    with batch():
        for emp_id in emp_ids:
            emp = query_one("SELECT id, department, rank, title, work_location, employment_type FROM employees WHERE id=?", (emp_id,))
            if not emp:
                continue
            for f, new in changes.items():
                cur = emp.get(f) or None
                if mode == "history":
                    old = ((from_values or {}).get(f) or "").strip() or None
                else:
                    old = cur
                    if cur == new:
                        skipped += 1
                        continue
                execute("INSERT INTO appointments(employee_id, appt_date, appt_type, from_value, to_value, note, field, status, "
                        "batch_title, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (emp_id, d.isoformat(), FIELDS[f][1], None if planned else old, new, note, f,
                         "예정" if planned else ("이력" if mode == "history" else "적용"), title, by or None))
                if mode == "apply" and not planned:
                    _apply(execute, emp_id, f, new)
                added += 1
    audit("hr", "인사발령 등록", title or d.isoformat(),
          f"{len(emp_ids)}명 · {added}건 · " + ("예정" if planned else ("이력만" if mode == "history" else "적용")))
    return {"added": added, "skipped": skipped, "planned": planned}


def apply_due() -> int:
    """발령일이 된 '예정' 발령을 적용 (인사·고용 정보 변경). 시작할 때와 1시간마다, 화면을 열 때 실행."""
    due = query("SELECT * FROM appointments WHERE status='예정' AND appt_date <= ? ORDER BY appt_date, id", (today().isoformat(),))
    n = 0
    for a in due:
        f = a.get("field")
        emp = query_one("SELECT * FROM employees WHERE id=?", (a["employee_id"],))
        if f not in FIELDS or not emp:
            execute("UPDATE appointments SET status='적용' WHERE id=?", (a["id"],))
            continue
        with batch():
            execute("UPDATE appointments SET status='적용', from_value=? WHERE id=?", (emp.get(f) or None, a["id"]))
            _apply(execute, a["employee_id"], f, a["to_value"])
        n += 1
    if n:
        audit("system", "예정 인사발령 적용", today().isoformat(), f"{n}건")
    return n


def delete(appt_id: int) -> str:
    a = query_one("SELECT a.*, e.name FROM appointments a JOIN employees e ON e.id=a.employee_id WHERE a.id=?", (appt_id,))
    if not a:
        return ""
    execute("DELETE FROM appointments WHERE id=?", (appt_id,))
    audit("hr", "인사발령 삭제", a["name"], f"{a['appt_date']} {a['appt_type']}" + (" (예정 취소)" if a.get("status") == "예정" else ""))
    return "예정 발령을 취소했습니다" if a.get("status") == "예정" else "발령 이력을 삭제했습니다 (인사·고용 정보는 그대로입니다)"


def notice_html(rs: list[dict], title: str) -> str:
    """인사발령 통지 문서 본문."""
    esc = html.escape
    s = get_settings()
    dates = sorted({r["appt_date"] for r in rs})
    when = dates[0] if len(dates) == 1 else f"{dates[0]} ~ {dates[-1]}"
    body = "".join(
        f"<tr><td>{i}</td><td>{esc(r.get('name') or '')}</td><td>{esc(r['appt_type'])}</td>"
        f"<td>{esc(r.get('from_value') or '-')}</td><td>{esc(r.get('to_value') or '-')}</td><td>{esc(r['appt_date'])}</td></tr>"
        for i, r in enumerate(sorted(rs, key=lambda x: (x["appt_date"], x.get("name") or "")), start=1))
    return (f"<h1 style='text-align:center;letter-spacing:.4em;margin:0 0 6mm'>인 사 발 령</h1>"
            f"<p style='text-align:center;margin:0 0 8mm'>{esc(title or '')}</p>"
            f"<p>아래와 같이 인사발령합니다.</p><p>발령일: {esc(when)}</p>"
            "<table style='width:100%;border-collapse:collapse;font-size:10.5pt' border='1' cellpadding='6'>"
            "<thead><tr style='background:#f0f0f0'><th>번호</th><th>성명</th><th>구분</th><th>변경 전</th><th>변경 후</th><th>발령일</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
            f"<p style='text-align:center;margin-top:16mm'>{esc(today().strftime('%Y년 %m월 %d일'))}</p>"
            f"<p style='text-align:center;font-size:15pt;font-weight:bold;margin-top:8mm'>{esc(s.get('company_name') or '')}</p>"
            f"<p style='text-align:center'>대표이사 {esc(s.get('ceo_name') or '')}</p>")


# ── 예정 발령 자동 적용 ──
_stop = threading.Event()
_thread = None


def _loop():
    while not _stop.wait(30):
        try:
            apply_due()
        except Exception:  # 일시 오류로 멈추지 않게
            log.exception("예정 인사발령 적용 오류")
        _stop.wait(3600)


def start_worker():
    global _thread
    if _thread:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-appoint", daemon=True)
    _thread.start()


def stop_worker():
    global _thread
    _stop.set()
    _thread = None
