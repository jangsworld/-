"""교육 이수 관리: 과정(주기·시간·대상) × 직원 이수 기록 → 연도별 이수 현황표."""
from collections import defaultdict
from datetime import date

from app.common import to_date, today
from app.db import audit, batch, execute, query, query_one
from app.modules.mgmt.common import employees_in_year
from app.modules.mgmt.constants import TRAINING_CYCLES, TRAINING_TARGETS


class TrainingError(ValueError):
    pass


def courses(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM training_courses" + (" WHERE is_active=1" if active_only else "")
    return query(sql + " ORDER BY is_active DESC, sort_order, id")


def course(cid: int) -> dict | None:
    return query_one("SELECT * FROM training_courses WHERE id=?", (cid,))


def periods(cycle: str, year: int) -> list[dict]:
    """그 해에 확인할 기간들: key, 이름, 시작, 끝."""
    y = int(year)
    if cycle == "반기":
        return [{"key": f"{y}-H1", "label": "상반기", "start": date(y, 1, 1), "end": date(y, 6, 30)},
                {"key": f"{y}-H2", "label": "하반기", "start": date(y, 7, 1), "end": date(y, 12, 31)}]
    if cycle == "분기":
        ends = {1: date(y, 3, 31), 2: date(y, 6, 30), 3: date(y, 9, 30), 4: date(y, 12, 31)}
        return [{"key": f"{y}-Q{q}", "label": f"{q}분기", "start": date(y, q * 3 - 2, 1), "end": ends[q]} for q in range(1, 5)]
    if cycle == "최초 1회":
        return [{"key": "ever", "label": "최초", "start": date(1900, 1, 1), "end": date(y, 12, 31)}]
    if cycle in ("2년", "3년"):
        n = int(cycle[0])
        return [{"key": f"{y}-{n}y", "label": f"{y - n + 1}~{y}", "start": date(y - n + 1, 1, 1), "end": date(y, 12, 31)}]
    return [{"key": str(y), "label": f"{y}년", "start": date(y, 1, 1), "end": date(y, 12, 31)}]


def targets(c: dict, year: int, emps: list[dict] | None = None) -> list[dict]:
    emps = emps if emps is not None else employees_in_year(year)
    if c["target"] == "지정 직원":
        ids = {r["employee_id"] for r in query("SELECT employee_id FROM training_targets WHERE course_id=?", (c["id"],))}
        return [e for e in emps if e["id"] in ids]
    return emps


def matrix(year: int, department: str = "") -> dict:
    t = today()
    emps = employees_in_year(year)
    cs = courses()
    recs = defaultdict(list)
    for r in query("SELECT * FROM training_records ORDER BY edu_date"):
        recs[(r["course_id"], r["employee_id"])].append(r)
    cells, summary = {}, []
    for c in cs:
        tg = targets(c, year, emps)
        ps = periods(c["cycle"], year)
        need = float(c["hours"] or 0)
        done_n = due_n = 0
        missing = []
        for e in tg:
            hd = to_date(e.get("hire_date"))
            plist, due_all, done_all = [], 0, 0
            for p in ps:
                rs = [r for r in recs.get((c["id"], e["id"]), []) if p["start"] <= (to_date(r["edu_date"]) or date.min) <= p["end"]]
                hours = sum(float(r["hours"] or 0) for r in rs)
                ok = bool(rs) and (hours >= need if need else True)
                applies = not (hd and hd > p["end"])    # 그 기간 뒤에 입사하면 해당 없음
                due = applies and (p["end"] < t or ok)  # 기간이 끝났거나 이미 이수
                plist.append({"label": p["label"], "hours": hours, "ok": ok, "due": due, "applies": applies, "end": p["end"],
                              "partial": bool(rs) and not ok})
                if due:
                    due_all += 1
                    done_all += 1 if ok else 0
            if due_all == 0:
                status = "upcoming"
            elif done_all == due_all:
                status = "done"
            elif done_all or any(x["partial"] for x in plist):
                status = "partial"
            else:
                status = "missing"
            last = max((r["edu_date"] for r in recs.get((c["id"], e["id"]), [])), default="")
            cells[(e["id"], c["id"])] = {"status": status, "periods": plist, "last": last}
            if status != "upcoming":
                due_n += 1
                done_n += 1 if status == "done" else 0
                if status != "done":
                    missing.append(e)
        nxt = next((p["end"] for p in ps if p["end"] >= t), None)
        summary.append({"course": c, "targets": len(tg), "due": due_n, "done": done_n, "missing": missing,
                        "rate": done_n / due_n * 100 if due_n else None, "next_end": nxt,
                        "pending": [e for e in tg if cells[(e["id"], c["id"])]["status"] == "upcoming"
                                    and not any(p["ok"] for p in cells[(e["id"], c["id"])]["periods"])]})
    rows = []
    for e in emps:
        if department and (e.get("department") or "") != department:
            continue
        row = {"employee": e, "cells": [cells.get((e["id"], c["id"])) for c in cs]}
        row["todo"] = sum(1 for x in row["cells"] if x and x["status"] in ("missing", "partial"))
        rows.append(row)
    return {"year": year, "courses": cs, "rows": rows, "summary": summary}


def records(year: int | None = None, course_id: int | None = None, employee_id: int | None = None) -> list[dict]:
    sql = ("SELECT r.*, c.name course, e.name, e.department FROM training_records r JOIN training_courses c ON c.id=r.course_id "
           "JOIN employees e ON e.id=r.employee_id WHERE 1=1")
    p = []
    if year:
        sql += " AND substr(r.edu_date,1,4)=?"
        p.append(str(year))
    if course_id:
        sql += " AND r.course_id=?"
        p.append(course_id)
    if employee_id:
        sql += " AND r.employee_id=?"
        p.append(employee_id)
    return query(sql + " ORDER BY r.edu_date DESC, e.name", p)


def add_records(cid: int, emp_ids: list[int], edu_date: str, hours, method: str, provider: str, note: str, by: str = "") -> int:
    c = course(cid)
    if not c:
        raise TrainingError("과정을 찾을 수 없습니다")
    d = to_date(edu_date)
    if not d:
        raise TrainingError("교육일을 넣어 주세요")
    try:
        h = float(hours or 0)
    except ValueError as ex:
        raise TrainingError("교육 시간은 숫자로 넣어 주세요") from ex
    ids = sorted({int(x) for x in emp_ids if str(x).isdigit()})
    if not ids:
        raise TrainingError("이수한 직원을 한 명 이상 고르세요")
    with batch():
        for eid in ids:
            execute("INSERT INTO training_records(course_id, employee_id, edu_date, hours, method, provider, note, created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)", (cid, eid, d.isoformat(), h, method or None, provider or None, note or None, by))
    audit("mgmt", "교육 이수 등록", c["name"], f"{d} {len(ids)}명")
    return len(ids)


def delete_record(rid: int):
    execute("DELETE FROM training_records WHERE id=?", (rid,))
    audit("mgmt", "교육 이수 삭제", str(rid), "")


def save_course(f: dict) -> int:
    name = (f.get("name") or "").strip()
    if not name:
        raise TrainingError("과정 이름을 넣어 주세요")
    if f.get("cycle") not in TRAINING_CYCLES or f.get("target") not in TRAINING_TARGETS:
        raise TrainingError("주기·대상을 목록에서 고르세요")
    try:
        hours = float(f.get("hours") or 0)
    except ValueError as ex:
        raise TrainingError("시간은 숫자로 넣어 주세요") from ex
    vals = (name, f.get("category") or "기타", f["cycle"], hours, f["target"], (f.get("basis") or "").strip() or None,
            (f.get("note") or "").strip() or None, 1 if f.get("is_active", "1") in ("1", "on", True, 1) else 0)
    cid = int(f["id"]) if str(f.get("id") or "").isdigit() else None
    dup = query_one("SELECT id FROM training_courses WHERE name=?", (name,))
    if dup and dup["id"] != cid:
        raise TrainingError("같은 이름의 과정이 있습니다")
    if cid:
        execute("UPDATE training_courses SET name=?, category=?, cycle=?, hours=?, target=?, basis=?, note=?, is_active=? WHERE id=?",
                (*vals, cid))
    else:
        cid = execute("INSERT INTO training_courses(name, category, cycle, hours, target, basis, note, is_active, sort_order) "
                      "VALUES (?,?,?,?,?,?,?,?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM training_courses))", vals)
    audit("mgmt", "교육 과정 저장", name, "")
    return cid


def target_ids(cid: int) -> set[int]:
    return {r["employee_id"] for r in query("SELECT employee_id FROM training_targets WHERE course_id=?", (cid,))}


def set_targets(cid: int, emp_ids: list[int]):
    with batch():
        execute("DELETE FROM training_targets WHERE course_id=?", (cid,))
        for eid in {int(x) for x in emp_ids if str(x).isdigit()}:
            execute("INSERT INTO training_targets(course_id, employee_id) VALUES (?,?)", (cid, eid))
    audit("mgmt", "교육 대상자 지정", str(cid), f"{len(emp_ids)}명")


def alerts(year: int | None = None, days: int = 60) -> list[dict]:
    """기한이 지났거나 가까운데 미이수자가 있는 과정."""
    t = today()
    out = []
    for s in matrix(year or t.year)["summary"]:
        c = s["course"]
        if s["missing"]:
            out.append({"course": c, "count": len(s["missing"]), "overdue": True, "due": None})
        if s["next_end"] and (s["next_end"] - t).days <= days and s["pending"]:
            out.append({"course": c, "count": len(s["pending"]), "overdue": False, "due": s["next_end"]})
    return out
