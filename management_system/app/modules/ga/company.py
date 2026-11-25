"""회사 관리 만기: 면허·인허가·등기·보험·인증·인감·법인카드 등 만기와 갱신 이력."""
import json
import logging
import re
import threading
from datetime import timedelta

from app.common import add_years, days_until, to_date, today
from app.db import audit, batch, execute, get_settings, query, query_one, set_setting
from app import company as co_mod
from app.modules.mgmt.constants import COMPANY_CATEGORIES, COMPANY_CYCLES, COMPANY_STATUSES


class CompanyError(ValueError):
    pass


CYCLE_YEARS = {"매년": 1, "2년": 2, "3년": 3, "5년": 5}


def items(category: str = "", status: str = "") -> list[dict]:
    sql, p = "SELECT * FROM ga_company_items WHERE 1=1" + co_mod.sql(""), []
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


def categories() -> list[str]:
    """면허·보험·인증 구분: 구분값 설정(COMPANY_ITEM)에서 고친 목록. 비어 있으면 기본 목록."""
    from app.db import get_codes
    return get_codes("COMPANY_ITEM") or list(COMPANY_CATEGORIES)


def clean_mails(text) -> list[str]:
    """알림 받을 메일 주소들 (쉼표·빈칸으로 구분, 5명까지). 틀리면 CompanyError."""
    mails = [m.strip() for m in re.split(r"[,;\s]+", str(text or "")) if m.strip()]
    if len(mails) > 5 or any(not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", m) or len(m) > 120 for m in mails):
        raise CompanyError("알림 받을 메일 주소를 확인해 주세요 (쉼표로 5명까지)")
    return mails


def allowed_mails(text, before: str = "") -> str:
    """알림 메일: 알림 부서 직원의 메일과, 이미 저장돼 있던 주소만. 알림 부서에서 고를 메일이 하나도 없으면 제한하지 않음."""
    from app.modules.ga import repo
    mails = list(dict.fromkeys(clean_mails(text)))
    staff = {e["mail"].lower() for e in repo.ga_staff() if e["mail"]}
    kept = {m.strip().lower() for m in str(before or "").split(",") if m.strip()}
    bad = [m for m in mails if staff and m.lower() not in staff | kept]
    if bad:
        raise CompanyError("만기 알림 메일은 알림 부서 직원 중에서 골라 주세요: " + ", ".join(bad[:2]))
    return ", ".join(mails)


def save(f: dict, by: str = "") -> int:
    name = (f.get("name") or "").strip()
    if not name:
        raise CompanyError("이름을 넣어 주세요")
    cats = categories()
    cat = f.get("category") or ("기타" if "기타" in cats else cats[0])
    if cat not in cats and not query_one("SELECT 1 x FROM ga_company_items WHERE category=? LIMIT 1", (cat,)):   # 예전에 쓰던 구분은 그대로 둘 수 있게
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
    before = get(int(f["id"])) if str(f.get("id") or "").isdigit() else None
    mails = allowed_mails(f.get("alert_email"), (before or {}).get("alert_email") or "")
    emp_ins = "1" if f.get("emp_insurance") else None
    if emp_ins and not (sd and ed):
        raise CompanyError("임직원 상해공제는 시작일과 만기일을 넣어 주세요")
    vals = (cat, name, (f.get("number") or "").strip() or None, (f.get("agency") or "").strip() or None,
            sd.isoformat() if sd else None, ed.isoformat() if ed else None, f.get("cycle") or "없음",
            (f.get("owner") or "").strip() or None, alert, f.get("status") or "유효", (f.get("note") or "").strip() or None)
    iid = int(f["id"]) if str(f.get("id") or "").isdigit() else None
    vals = (*vals, emp_ins, mails or None)
    if iid:
        execute("UPDATE ga_company_items SET category=?, name=?, number=?, agency=?, start_date=?, expire_date=?, cycle=?, owner=?, "
                "alert_days=?, status=?, note=?, emp_insurance=?, alert_email=? WHERE id=?", (*vals, iid))
        if f.get("company") in co_mod.IDS:
            execute("UPDATE ga_company_items SET company=? WHERE id=?", (co_mod.pick(f.get("company")), iid))
    else:
        iid = execute("INSERT INTO ga_company_items(category, name, number, agency, start_date, expire_date, cycle, owner, "
                      "alert_days, status, note, emp_insurance, alert_email, company) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*vals, co_mod.pick(f.get("company"))))
    audit("ga", "회사 관리 만기 저장", name, f"{cat} {vals[5] or ''}")
    return iid


# ── 임직원 상해공제: 여기 한 번 넣으면 재직 직원 인사기록카드 「상해공제」에 반영 ──
def insurance_excluded() -> set[int]:
    """따로 관리하는 직원(일괄 반영에서 뺌). 「상해공제 반영 대상」 화면에서 체크를 푼 직원."""
    try:
        return {int(x) for x in json.loads(get_settings().get("insurance_excluded") or "[]")}
    except (ValueError, TypeError):
        return set()


def reflect_insurance(iid: int) -> dict | None:
    """'임직원 상해공제'로 표시한 항목을 재직 직원에게 반영. 표시가 없거나 유효하지 않으면 None."""
    from app.modules.hr import repo as hr_repo
    it = get(iid)
    if not it or not it.get("emp_insurance") or it["status"] not in ("유효", "갱신중") or not (it.get("start_date") and it.get("expire_date")):
        return None
    try:
        prev = json.loads(get_settings().get("insurance_common") or "{}")
    except ValueError:
        prev = {}
    same = prev.get("product") == it["name"]              # 공제료·가입일·비고는 반영 대상 화면에서 넣은 값을 이어 씀
    data = {"product": it["name"], "insurer": it.get("agency") or "", "policy_no": it.get("number") or "",
            "start_date": it["start_date"], "end_date": it["expire_date"],
            "joined_on": (prev.get("joined_on") if same and prev.get("start_date") == it["start_date"] else "") or it["start_date"],
            "premium": prev.get("premium", "") if same else "", "note": prev.get("note", "") if same else ""}
    set_setting("insurance_common", json.dumps(data, ensure_ascii=False))
    t, skip = today().isoformat(), insurance_excluded()
    ids = [e["id"] for e in query("SELECT id, resign_date FROM employees")
           if (not e.get("resign_date") or e["resign_date"] >= t) and e["id"] not in skip]
    r = hr_repo.bulk_insurance(data, ids)
    r["skipped"] = len(skip)
    return r


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
        if it.get("emp_insurance") and old:        # 상해공제 갱신: 새 보장 기간은 이전 만기 다음 날부터
            execute("UPDATE ga_company_items SET start_date=? WHERE id=?", ((old + timedelta(days=1)).isoformat(), iid))
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
                    "dday": r["dday"], "link": f"/ga/contracts/item/{r['id']}", "emp_id": None})
    return out


# ── 만기 알림 메일: 담당자 메일을 넣은 항목은 '미리 알림(일)' 전, 7일 전, 만기 당일에 한 번씩 ──
log = logging.getLogger("bms.company")
_stop = threading.Event()
_thread = None
CHECK_HOURS = 3
STAGES = ["first", "d7", "d0"]


def _stage(dday: int, alert_days: int) -> str | None:
    if dday < 0:
        return None                      # 지난 것은 대시보드에서 계속 보임 (메일을 날마다 보내지 않음)
    if dday == 0:
        return "d0"
    if dday <= 7:
        return "d7"
    return "first" if dday <= alert_days else None


def due_mails() -> list[dict]:
    """지금 보낼 알림 목록 (이미 같은 단계를 보낸 항목은 빠짐)."""
    out = []
    for r in items(status="관리중"):
        if not r.get("alert_email") or r["dday"] is None:
            continue
        st = _stage(r["dday"], int(r.get("alert_days") or 60))
        if not st:
            continue
        sent = (r.get("alert_sent") or "").split(":")
        if len(sent) == 2 and sent[0] == r["expire_date"] and sent[1] in STAGES and STAGES.index(sent[1]) >= STAGES.index(st):
            continue
        out.append({**r, "stage": st})
    return out


def send_due_mails() -> int:
    """알림 메일 보내기. 메일 설정이 없으면 보내지 않고 0."""
    from app import mailer
    if not mailer.configured():
        return 0
    base = (get_settings().get("public_url") or "").rstrip("/")
    n = 0
    for r in due_mails():
        when = "오늘 만기입니다" if r["dday"] == 0 else f"만기까지 {r['dday']}일 남았습니다"
        subject = f"[BMS시스템] 만기 알림: {r['name']} ({r['expire_date']})"
        lines = ["회사 관리 만기 알림입니다.", "", f"항목: {r['name']}", f"구분: {r['category']}",
                 f"만기·기한: {r['expire_date']} ({when})"]
        if r.get("owner"):
            lines.append(f"담당: {r['owner']}")
        lines += ["", "갱신을 마치면 BMS시스템 「총무관리 → 계약·만기 관리」에서 [갱신]을 눌러 주세요."]
        if base:
            lines.append(f"{base}/ga/contracts/item/{r['id']}")
        ok = False
        for to in [m.strip() for m in r["alert_email"].split(",") if m.strip()]:
            try:
                mailer.send(to, subject, "\n".join(lines), plain=True)
                ok = True
            except Exception:  # noqa: BLE001  (한 명 실패해도 나머지는 보냄)
                log.exception("만기 알림 메일 실패: 항목 #%s", r["id"])
        if ok:
            execute("UPDATE ga_company_items SET alert_sent=? WHERE id=?", (f"{r['expire_date']}:{r['stage']}", r["id"]))
            audit("ga", "만기 알림 메일", r["name"], f"D-{r['dday']}")
            n += 1
    return n + _send_contract_mails(base)


CONTRACT_ALERT_DAYS = 30


def contract_due_mails() -> list[dict]:
    """알림 메일을 넣은 유효 계약 중 지금 보낼 것. 기준일은 해지·갱신 통보기한(없으면 만료일):
    그 30일 전, 7일 전, 당일에 한 번씩."""
    from app.modules.ga import repo, services
    out = []
    for c in repo.list_contracts(status="유효"):
        if not c.get("alert_email") or not c.get("end_date"):
            continue
        c = services.contract_view(c)
        target, dday = c["notice_date"] or c["end_date"], c["notice_dday"]
        if dday is None:
            continue
        st = _stage(dday, CONTRACT_ALERT_DAYS)
        if not st:
            continue
        sent = (c.get("alert_sent") or "").split(":")
        if len(sent) == 2 and sent[0] == target and sent[1] in STAGES and STAGES.index(sent[1]) >= STAGES.index(st):
            continue
        out.append({**c, "stage": st, "target": target, "target_dday": dday})
    return out


def _send_contract_mails(base: str) -> int:
    from app import mailer
    n = 0
    for c in contract_due_mails():
        notice = bool(c.get("notice_days"))
        what = "해지·갱신 통보기한" if notice else "만료일"
        when = f"오늘이 {what}입니다" if c["target_dday"] == 0 else f"{what}까지 {c['target_dday']}일 남았습니다"
        subject = f"[BMS시스템] 계약 만기 알림: {c['title']} ({c['target']})"
        lines = ["계약 만기 알림입니다.", "", f"계약: {c['title']}", f"구분: {c['category']}", f"상대방: {c.get('counterparty') or ''}",
                 f"계약기간: {c.get('start_date') or ''} ~ {c['end_date']}"]
        if notice:
            lines.append(f"해지·갱신 통보기한: {c['target']} (만료 {c['notice_days']}일 전)")
        lines.append(when + ("  ※ 자동갱신 계약: 통보기한을 넘기면 같은 조건으로 연장됩니다" if c.get("auto_renew") else ""))
        lines += ["", "BMS시스템 「총무관리 → 계약·만기 관리」에서 확인해 주세요."]
        if base:
            lines.append(f"{base}/ga/contracts/{c['id']}")
        ok = False
        for to in [m.strip() for m in c["alert_email"].split(",") if m.strip()]:
            try:
                mailer.send(to, subject, "\n".join(lines), plain=True)
                ok = True
            except Exception:  # noqa: BLE001
                log.exception("계약 만기 알림 메일 실패: 계약 #%s", c["id"])
        if ok:
            execute("UPDATE ga_contracts SET alert_sent=? WHERE id=?", (f"{c['target']}:{c['stage']}", c["id"]))
            audit("ga", "계약 만기 알림 메일", c["title"], f"D-{c['target_dday']}")
            n += 1
    return n


def _loop():
    while not _stop.wait(120):
        try:
            send_due_mails()
        except Exception:  # 일시 오류로 멈추지 않게
            log.exception("만기 알림 점검 오류")
        _stop.wait(CHECK_HOURS * 3600)


def start_worker():
    global _thread
    if _thread:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-company-alert", daemon=True)
    _thread.start()


def stop_worker():
    global _thread
    _stop.set()
    _thread = None
