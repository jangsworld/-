"""인센티브 예산: 연간 총예산 → 부서별 배정 → 지급 기록 → 잔액·소진율."""
from collections import defaultdict

from app.common import to_date, today
from app.db import audit, batch, execute, query, query_one
from app.modules.mgmt.common import depts
from app.security.crypto import enc

WARN_RATE = 80      # 소진율 경고(%)


class IncentiveError(ValueError):
    pass


def years() -> list[int]:
    ys = {r["year"] for r in query("SELECT year FROM incentive_years")}
    ys |= {r["year"] for r in query("SELECT DISTINCT year FROM incentive_payments")}
    return sorted(ys)


def payments(year: int, department: str = "") -> list[dict]:
    sql = ("SELECT p.*, e.name, e.emp_no, e.rank FROM incentive_payments p LEFT JOIN employees e ON e.id=p.employee_id "
           "WHERE p.year=?")
    params = [int(year)]
    if department:
        sql += " AND p.department=?"
        params.append(department)
    rows = query(sql + " ORDER BY p.pay_date DESC, p.id DESC", params)
    for r in rows:
        r["amount"] = int(r["amount"] or 0)
    return rows


def employee_totals(year: int) -> dict[int, int]:
    out = defaultdict(int)
    for p in payments(year):
        if p["employee_id"]:
            out[p["employee_id"]] += p["amount"]
    return dict(out)


def dept_unassigned_paid(year: int) -> dict[str, int]:
    """직원을 정하지 않고 부서로만 지급한 금액."""
    out = defaultdict(int)
    for p in payments(year):
        if not p["employee_id"]:
            out[p["department"]] += p["amount"]
    return dict(out)


def overview(year: int) -> dict:
    y = query_one("SELECT * FROM incentive_years WHERE year=?", (int(year),)) or {"total": 0, "note": ""}
    budgets = {r["department"]: r for r in query("SELECT * FROM incentive_budgets WHERE year=?", (int(year),))}
    pays = payments(year)
    paid = defaultdict(int)
    monthly = [0] * 12
    for p in pays:
        paid[p["department"]] += p["amount"]
        d = to_date(p["pay_date"])
        if d and d.year == int(year):
            monthly[d.month - 1] += p["amount"]
    names = [d for d in depts() if d in budgets or d in paid]
    names += [d for d in list(budgets) + list(paid) if d not in names]
    rows = []
    for d in names:
        b = int((budgets.get(d) or {}).get("amount") or 0)
        pd = paid.get(d, 0)
        rate = pd / b * 100 if b else (100.0 if pd else None)
        rows.append({"department": d, "budget": b, "paid": pd, "remain": b - pd, "rate": rate,
                     "level": "danger" if (rate or 0) > 100 else ("warning" if (rate or 0) >= WARN_RATE else "ok"),
                     "note": (budgets.get(d) or {}).get("note") or ""})
    total = int(y.get("total") or 0)
    assigned = sum(r["budget"] for r in rows)
    paid_sum = sum(r["paid"] for r in rows)
    cum, acc = [], 0
    t = today()
    for i, m in enumerate(monthly):
        acc += m
        cum.append(acc if not (int(year) == t.year and i + 1 > t.month) else None)   # 올해 남은 달은 비움
    emp = defaultdict(lambda: {"amount": 0, "count": 0})
    for p in pays:
        if p["employee_id"]:
            e = emp[p["employee_id"]]
            e.update(name=p["name"], department=p["department"], rank=p.get("rank"))
            e["amount"] += p["amount"]
            e["count"] += 1
    return {
        "year": int(year), "total": total, "note": y.get("note") or "", "assigned": assigned,
        "unassigned": total - assigned, "paid": paid_sum, "remain": total - paid_sum,
        "rate": paid_sum / total * 100 if total else None,
        "rows": rows, "monthly": monthly, "cumulative": cum, "payments": pays,
        "people": sorted(({"employee_id": k, **v} for k, v in emp.items()), key=lambda x: -x["amount"]),
        "warn": [r for r in rows if r["level"] != "ok"],
    }


def save_plan(year: int, total: int, note: str, budgets: dict[str, int], by: str = ""):
    if total < 0 or any(v < 0 for v in budgets.values()):
        raise IncentiveError("금액은 0 이상이어야 합니다")
    with batch():
        execute("INSERT INTO incentive_years(year, total, note) VALUES (?,?,?) "
                "ON CONFLICT(year) DO UPDATE SET total=excluded.total, note=excluded.note", (int(year), int(total), note))
        for d, amt in budgets.items():
            if not d:
                continue
            execute("INSERT INTO incentive_budgets(year, department, amount) VALUES (?,?,?) "
                    "ON CONFLICT(year, department) DO UPDATE SET amount=excluded.amount", (int(year), d, int(amt)))
    audit("mgmt", "인센티브 예산 저장", str(year), f"총 {total:,}원")


def add_payments(year: int, rows: list[dict], by: str = "") -> int:
    """rows: [{pay_date, department, employee_id, amount, reason, note}] — 한꺼번에 검사 후 저장."""
    clean = []
    emp_dept = {r["id"]: r["department"] for r in query("SELECT id, department FROM employees")}
    for i, r in enumerate(rows, 1):
        amt = int(str(r.get("amount") or "0").replace(",", "") or 0)
        if amt <= 0:
            continue
        d = to_date(r.get("pay_date"))
        if not d or d.year != int(year):
            raise IncentiveError(f"{i}번째 줄: 지급일은 {year}년 날짜여야 합니다")
        eid = int(r["employee_id"]) if str(r.get("employee_id") or "").isdigit() else None
        if eid and eid not in emp_dept:
            raise IncentiveError(f"{i}번째 줄: 직원을 찾을 수 없습니다")
        dept = (r.get("department") or "").strip() or (emp_dept.get(eid) if eid else "")
        if not dept:
            raise IncentiveError(f"{i}번째 줄: 부서를 정해 주세요")
        clean.append((int(year), d.isoformat(), dept, eid, enc(amt), (r.get("reason") or "").strip(),
                      (r.get("note") or "").strip(), by))
    if not clean:
        raise IncentiveError("지급액을 넣은 줄이 없습니다")
    with batch():
        for c in clean:
            execute("INSERT INTO incentive_payments(year, pay_date, department, employee_id, amount, reason, note, created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)", c)
    audit("mgmt", "인센티브 지급 등록", str(year), f"{len(clean)}건")
    return len(clean)


def delete_payment(pid: int):
    execute("DELETE FROM incentive_payments WHERE id=?", (pid,))
    audit("mgmt", "인센티브 지급 삭제", str(pid), "")


def paid_by_year() -> dict[int, int]:
    out = defaultdict(int)
    for r in query("SELECT year, amount FROM incentive_payments"):
        out[r["year"]] += int(r["amount"] or 0)
    return dict(out)
