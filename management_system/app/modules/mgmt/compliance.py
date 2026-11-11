"""법정의무 일정: 신고·납부·회의·점검(직접 관리) + 교육 이수 기한 + 회사 관리 만기 + 계약 만료를 한 달력에."""
import calendar
from datetime import date

from app.common import to_date, today
from app.db import audit, execute, get_settings, query, query_one
from app.modules.mgmt import training
from app.modules.mgmt.constants import COMPLIANCE_CYCLES


class ComplianceError(ValueError):
    pass


def _d(y: int, m: int, day: int | None) -> date:
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(int(day or last), last))


def occurrences(t: dict, year: int) -> list[dict]:
    y = int(year)
    c = t["cycle"]
    if c == "매월":
        return [{"period": f"{y}-{m:02d}", "label": f"{m}월분", "due": _d(y, m, t["day"])} for m in range(1, 13)]
    if c == "분기":
        return [{"period": f"{y}-Q{q}", "label": f"{q}분기", "due": _d(y, q * 3, t["day"])} for q in range(1, 5)]
    if c == "반기":
        return [{"period": f"{y}-H1", "label": "상반기", "due": _d(y, 6, t["day"])},
                {"period": f"{y}-H2", "label": "하반기", "due": _d(y, 12, t["day"])}]
    if c == "한 번":
        d = to_date(t.get("due_date"))
        return [{"period": "once", "label": "", "due": d}] if d and d.year == y else []
    if not t.get("month"):
        return []
    return [{"period": str(y), "label": "", "due": _d(y, int(t["month"]), t["day"])}]


def tasks(active_only: bool = False) -> list[dict]:
    return query("SELECT * FROM compliance_tasks" + (" WHERE is_active=1" if active_only else "") +
                 " ORDER BY is_active DESC, COALESCE(month, 0), COALESCE(day, 0), id")


def _status(due: date, done: bool, soon_days: int = 14) -> str:
    if done:
        return "done"
    left = (due - today()).days
    if left < 0:
        return "overdue"
    return "soon" if left <= soon_days else "upcoming"


def calendar_year(year: int, sources=("의무", "교육", "회사관리", "계약")) -> list[dict]:
    y = int(year)
    ev = []
    if "의무" in sources:
        done = {(r["task_id"], r["period"]): r for r in query("SELECT * FROM compliance_done")}
        since = to_date(get_settings().get("compliance_since"))
        for t in tasks(active_only=True):
            for o in occurrences(t, y):
                d = done.get((t["id"], o["period"]))
                st = _status(o["due"], bool(d))
                if st == "overdue" and since and o["due"] < since:
                    st = "before"                   # BMS로 관리하기 전 일정
                ev.append({"date": o["due"], "title": t["title"] + (f" ({o['label']})" if o["label"] else ""),
                           "category": t["category"], "source": "의무", "status": st,
                           "task_id": t["id"], "period": o["period"], "done": d, "basis": t.get("basis"), "note": t.get("note")})
    if "교육" in sources:
        m = training.matrix(y)
        for ci, s in enumerate(m["summary"]):
            c = s["course"]
            if c["cycle"] == "최초 1회" or not s["targets"]:
                continue                            # 최초 1회 교육은 기한이 없어 이수 현황표에서만 봅니다
            for pi, p in enumerate(training.periods(c["cycle"], y)):
                cells = [r["cells"][ci]["periods"][pi] for r in m["rows"] if r["cells"][ci]]
                applies = [x for x in cells if x["applies"]]
                miss = sum(1 for x in applies if not x["ok"])
                title = c["name"] + (f" ({p['label']})" if c["cycle"] in ("반기", "분기") else "") + " 이수 기한"
                ev.append({"date": p["end"], "title": title, "category": c["category"], "source": "교육",
                           "status": _status(p["end"], miss == 0),
                           "detail": f"미이수 {miss}명 / 대상 {len(applies)}명" if miss else f"대상 {len(applies)}명 모두 이수",
                           "link": "/mgmt/training?year=%d" % y})
    if "회사관리" in sources:
        for r in query("SELECT * FROM ga_company_items WHERE status IN ('유효','갱신중') AND expire_date LIKE ?", (f"{y}-%",)):
            d = to_date(r["expire_date"])
            if d:
                ev.append({"date": d, "title": f"{r['name']} 만기·갱신", "category": r["category"], "source": "회사관리",
                           "status": _status(d, False, int(r.get("alert_days") or 60)), "detail": r.get("agency") or "",
                           "link": "/ga/company"})
    if "계약" in sources:
        for r in query("SELECT * FROM ga_contracts WHERE status='유효' AND end_date LIKE ?", (f"{y}-%",)):
            d = to_date(r["end_date"])
            if d:
                ev.append({"date": d, "title": f"계약 만료: {r['title']}", "category": r["category"], "source": "계약",
                           "status": _status(d, False, int(r.get("notice_days") or 30)), "detail": r.get("counterparty") or "",
                           "link": f"/ga/contracts"})
    ev.sort(key=lambda e: (e["date"], e["source"], e["title"]))
    return ev


def mark_done(task_id: int, period: str, done_date: str, by: str, note: str = ""):
    if not query_one("SELECT 1 FROM compliance_tasks WHERE id=?", (task_id,)):
        raise ComplianceError("일정을 찾을 수 없습니다")
    d = to_date(done_date) or today()
    execute("INSERT INTO compliance_done(task_id, period, done_date, done_by, note) VALUES (?,?,?,?,?) "
            "ON CONFLICT(task_id, period) DO UPDATE SET done_date=excluded.done_date, done_by=excluded.done_by, note=excluded.note",
            (task_id, period, d.isoformat(), by, note or None))
    audit("mgmt", "법정의무 처리", str(task_id), period)


def undo_done(task_id: int, period: str):
    execute("DELETE FROM compliance_done WHERE task_id=? AND period=?", (task_id, period))
    audit("mgmt", "법정의무 처리 취소", str(task_id), period)


def save_task(f: dict) -> int:
    title = (f.get("title") or "").strip()
    if not title:
        raise ComplianceError("제목을 넣어 주세요")
    cyc = f.get("cycle")
    if cyc not in COMPLIANCE_CYCLES:
        raise ComplianceError("주기를 목록에서 고르세요")

    def num(k, lo, hi):
        v = str(f.get(k) or "").strip()
        if not v:
            return None
        if not v.isdigit() or not lo <= int(v) <= hi:
            raise ComplianceError("월은 1~12, 일은 1~31로 넣어 주세요")
        return int(v)
    month, day = num("month", 1, 12), num("day", 1, 31)
    due = to_date(f.get("due_date"))
    if cyc == "매년" and not month:
        raise ComplianceError("매년 일정은 기한 월을 넣어 주세요")
    if cyc == "한 번" and not due:
        raise ComplianceError("한 번 하는 일정은 기한 날짜를 넣어 주세요")
    vals = (title, f.get("category") or "기타", cyc, month, day, due.isoformat() if due else None,
            (f.get("basis") or "").strip() or None, (f.get("owner") or "").strip() or None, (f.get("note") or "").strip() or None,
            1 if f.get("is_active", "1") in ("1", "on") else 0)
    tid = int(f["id"]) if str(f.get("id") or "").isdigit() else None
    if tid:
        execute("UPDATE compliance_tasks SET title=?, category=?, cycle=?, month=?, day=?, due_date=?, basis=?, owner=?, note=?, "
                "is_active=? WHERE id=?", (*vals, tid))
    else:
        tid = execute("INSERT INTO compliance_tasks(title, category, cycle, month, day, due_date, basis, owner, note, is_active) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
    audit("mgmt", "법정의무 일정 저장", title, cyc)
    return tid


def delete_task(tid: int):
    execute("DELETE FROM compliance_tasks WHERE id=?", (tid,))
    audit("mgmt", "법정의무 일정 삭제", str(tid), "")


def upcoming(days: int = 30) -> list[dict]:
    """대시보드용: 지난 미처리 + days 안의 일정."""
    t = today()
    out = []
    years = [t.year] + ([t.year + 1] if (date(t.year, 12, 31) - t).days < days else [])
    for y in years:
        for e in calendar_year(y):
            if e["status"] in ("done", "before"):
                continue
            if e["status"] == "overdue" or (e["date"] - t).days <= days:
                out.append(e)
    return sorted(out, key=lambda e: e["date"])
