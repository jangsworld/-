"""회사 관리 만기: 면허·인허가·등기·보험·인증·인감·법인카드 등 만기와 갱신 이력."""
from app.common import add_years, days_until, to_date, today
from app.db import audit, batch, execute, query, query_one
from app.modules.mgmt.constants import COMPANY_CATEGORIES, COMPANY_CYCLES, COMPANY_STATUSES


class CompanyError(ValueError):
    pass


CYCLE_YEARS = {"매년": 1, "2년": 2, "3년": 3, "5년": 5}


def items(category: str = "", status: str = "") -> list[dict]:
    sql, p = "SELECT * FROM ga_company_items WHERE 1=1", []
    if category:
        sql += " AND category=?"
        p.append(category)
    if status == "관리중":
        sql += " AND status IN ('유효','갱신중')"
    elif status:
        sql += " AND status=?"
        p.append(status)
    rows = query(sql + " ORDER BY CASE WHEN expire_date IS NULL OR expire_date='' THEN 1 ELSE 0 END, expire_date, name", p)
    for r in rows:
        r["dday"] = days_until(r["expire_date"]) if r.get("expire_date") else None
        r["alert"] = r["status"] in ("유효", "갱신중") and r["dday"] is not None and r["dday"] <= int(r.get("alert_days") or 60)
    return rows


def get(iid: int) -> dict | None:
    return query_one("SELECT * FROM ga_company_items WHERE id=?", (iid,))


def save(f: dict, by: str = "") -> int:
    name = (f.get("name") or "").strip()
    if not name:
        raise CompanyError("이름을 넣어 주세요")
    cat = f.get("category") or "기타"
    if cat not in COMPANY_CATEGORIES:
        raise CompanyError("구분을 목록에서 고르세요")
    if (f.get("cycle") or "없음") not in COMPANY_CYCLES or (f.get("status") or "유효") not in COMPANY_STATUSES:
        raise CompanyError("주기·상태를 목록에서 고르세요")
    sd, ed = to_date(f.get("start_date")), to_date(f.get("expire_date"))
    if sd and ed and ed < sd:
        raise CompanyError("만기일이 시작일보다 빠릅니다")
    try:
        alert = int(f.get("alert_days") or 60)
    except ValueError as ex:
        raise CompanyError("알림 일수는 숫자로 넣어 주세요") from ex
    vals = (cat, name, (f.get("number") or "").strip() or None, (f.get("agency") or "").strip() or None,
            sd.isoformat() if sd else None, ed.isoformat() if ed else None, f.get("cycle") or "없음",
            (f.get("owner") or "").strip() or None, alert, f.get("status") or "유효", (f.get("note") or "").strip() or None)
    iid = int(f["id"]) if str(f.get("id") or "").isdigit() else None
    if iid:
        execute("UPDATE ga_company_items SET category=?, name=?, number=?, agency=?, start_date=?, expire_date=?, cycle=?, owner=?, "
                "alert_days=?, status=?, note=? WHERE id=?", (*vals, iid))
    else:
        iid = execute("INSERT INTO ga_company_items(category, name, number, agency, start_date, expire_date, cycle, owner, "
                      "alert_days, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
    audit("ga", "회사 관리 만기 저장", name, f"{cat} {vals[5] or ''}")
    return iid


def delete(iid: int):
    it = get(iid)
    execute("DELETE FROM ga_company_items WHERE id=?", (iid,))
    audit("ga", "회사 관리 만기 삭제", (it or {}).get("name", str(iid)), "")


def next_expire(it: dict):
    d = to_date(it.get("expire_date"))
    n = CYCLE_YEARS.get(it.get("cycle") or "")
    return add_years(d, n) if (d and n) else None


def renew(iid: int, new_expire: str, done_date: str, note: str, by: str = ""):
    it = get(iid)
    if not it:
        raise CompanyError("항목을 찾을 수 없습니다")
    ne = to_date(new_expire) or next_expire(it)
    if not ne:
        raise CompanyError("새 만기일을 넣어 주세요")
    old = to_date(it.get("expire_date"))
    if old and ne <= old:
        raise CompanyError("새 만기일은 지금 만기일보다 뒤여야 합니다")
    with batch():
        execute("INSERT INTO ga_company_history(item_id, old_expire, new_expire, done_date, done_by, note) VALUES (?,?,?,?,?,?)",
                (iid, it.get("expire_date"), ne.isoformat(), (to_date(done_date) or today()).isoformat(), by, note or None))
        execute("UPDATE ga_company_items SET expire_date=?, status='유효' WHERE id=?", (ne.isoformat(), iid))
    audit("ga", "회사 관리 갱신", it["name"], f"{it.get('expire_date')} → {ne}")


def history(iid: int | None = None) -> list[dict]:
    sql = "SELECT h.*, i.name FROM ga_company_history h JOIN ga_company_items i ON i.id=h.item_id"
    p = []
    if iid:
        sql += " WHERE h.item_id=?"
        p.append(iid)
    return query(sql + " ORDER BY h.done_date DESC, h.id DESC LIMIT 200", p)


def alerts() -> list[dict]:
    """총무 대시보드 알림 형식."""
    out = []
    for r in items(status="관리중"):
        if not r["alert"]:
            continue
        lvl = "danger" if r["dday"] < 0 else ("warning" if r["dday"] <= 30 else "info")
        msg = (f"만기 {-r['dday']}일 지남" if r["dday"] < 0 else f"만기 D-{r['dday']}") + f" ({r['expire_date']})"
        if r["status"] == "갱신중":
            msg += " · 갱신 진행 중"
        out.append({"level": lvl, "category": "회사관리", "name": f"{r['name']}", "message": msg,
                    "dday": r["dday"], "link": "/ga/company", "emp_id": None})
    return out
