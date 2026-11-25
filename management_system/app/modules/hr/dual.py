"""프로젝트 이중가입: 프로젝트 때문에 다른 사업장에도 4대보험이 가입되는 직원의 현황.

연봉은 바꾸지 않습니다(이사님 결정). 급여가 나뉘는 경우의 금액은 이 기록에만 남기고,
4대보험 기준 금액(건강·연금·고용 세 칸)은 담당자가 버튼을 눌렀을 때만 바꿉니다.
"""
import json

from app.common import days_until, to_date, today
from app.db import audit, execute, query, query_one
from app import company
from app.security.crypto import enc

REASONS = ["발주처 요구", "상주 인력 등록", "기술자 배치", "공동수급(컨소시엄)", "기타"]
INSURANCES = [("ins_pension", "국민연금"), ("ins_health", "건강보험"), ("ins_employ", "고용보험"), ("ins_accident", "산재보험")]
PAY_TYPES = ["그대로", "조정"]            # 그대로: 금액 변동 없이 사업장만 이중 / 조정: 우리 회사 금액이 줄고 상대 사업장이 나눠 지급
# 처리 체크: (열쇠, 이름, 급여 조정일 때만?)
CHECK_START = [("s_other", "상대 사업장 4대보험 취득 확인", False), ("s_report", "우리 회사 보수월액 변경 신고", True),
               ("s_ecount", "이카운트 금액 수정", True), ("s_pay", "급여 반영 확인", True)]
CHECK_END = [("e_other", "상대 사업장 4대보험 상실 확인", False), ("e_report", "우리 회사 보수월액 원복 신고", True),
             ("e_ecount", "이카운트 금액 원복", True), ("e_pay", "급여 원복 확인", True)]
ALERT_DAYS = 30


class DualError(ValueError):
    pass


def _decorate(r: dict) -> dict:
    t = today().isoformat()
    try:
        r["checks"] = json.loads(r.get("checks") or "{}")
    except ValueError:
        r["checks"] = {}
    for k in ("our_before", "our_after", "other_pay"):
        r[k] = int(r[k]) if str(r.get(k) or "").isdigit() else None
    ended = bool(r.get("actual_end")) and r["actual_end"] <= t
    r["state"] = "종료" if ended else ("예정" if r["start_date"] > t else "진행중")
    r["dday"] = days_until(r["end_date"]) if r.get("end_date") and not ended else None
    r["overdue"] = r["state"] == "진행중" and r["dday"] is not None and r["dday"] < 0
    r["ending"] = r["state"] == "진행중" and r["dday"] is not None and 0 <= r["dday"] <= ALERT_DAYS
    r["insurances"] = [name for key, name in INSURANCES if r.get(key)]
    adj = r.get("pay_type") == "조정"
    r["todo_start"] = [(k, n) for k, n, only_adj in CHECK_START if adj or not only_adj]
    r["todo_end"] = [(k, n) for k, n, only_adj in CHECK_END if adj or not only_adj]
    need = r["todo_start"] if r["state"] != "예정" else []
    if r["state"] == "종료" or r["overdue"] or r["ending"]:
        need = need + r["todo_end"]
    r["todo_left"] = [n for k, n in need if not r["checks"].get(k)]
    return r


def rows(state: str = "", employee_id: int | None = None) -> list[dict]:
    sql = ("SELECT d.*, e.name, e.emp_no, e.department, e.rank FROM dual_enrollments d "
           "JOIN employees e ON e.id=d.employee_id WHERE 1=1")
    p = []
    if not employee_id:
        sql += company.sql("e")
    if employee_id:
        sql += " AND d.employee_id=?"
        p.append(employee_id)
    out = [_decorate(r) for r in query(sql + " ORDER BY d.start_date DESC, d.id DESC", p)]
    if state == "정리필요":
        return [r for r in out if r["overdue"] or (r["state"] == "종료" and r["todo_left"])]
    return [r for r in out if not state or r["state"] == state]


def get(did: int) -> dict | None:
    r = query_one("SELECT d.*, e.name FROM dual_enrollments d JOIN employees e ON e.id=d.employee_id WHERE d.id=?", (did,))
    return _decorate(r) if r else None


def active_map() -> dict[int, dict]:
    """직원 id → 지금 진행 중인 이중가입 (기술자 현황·인사기록카드 표시용)."""
    out = {}
    for r in rows("진행중"):
        out.setdefault(r["employee_id"], r)
    return out


def _money(v, label) -> int | None:
    s = str(v or "").replace(",", "").strip()
    if not s:
        return None
    if not s.isdigit() or int(s) > 10**9:
        raise DualError(f"{label}은(는) 숫자로 넣어 주세요")
    return int(s)


def save(f: dict) -> int:
    emp_id = int(f["employee_id"]) if str(f.get("employee_id") or "").isdigit() else 0
    if not query_one("SELECT 1 x FROM employees WHERE id=?", (emp_id,)):
        raise DualError("직원을 골라 주세요")
    workplace = (f.get("workplace") or "").strip()[:100]
    if not workplace:
        raise DualError("상대 사업장 이름을 넣어 주세요")
    sd, ed, ae = to_date(f.get("start_date")), to_date(f.get("end_date")), to_date(f.get("actual_end"))
    if not sd:
        raise DualError("시작일을 넣어 주세요")
    if (ed and ed < sd) or (ae and ae < sd):
        raise DualError("종료일이 시작일보다 빠릅니다")
    reason = f.get("reason") or "기타"
    pay_type = f.get("pay_type") or "그대로"
    if reason not in REASONS or pay_type not in PAY_TYPES:
        raise DualError("사유·급여 형태를 목록에서 고르세요")
    before, after, other = (_money(f.get("our_before"), "조정 전 금액"), _money(f.get("our_after"), "조정 후 금액"),
                            _money(f.get("other_pay"), "상대 사업장 금액"))
    if pay_type == "조정" and not (before and after):
        raise DualError("급여를 조정하는 경우 우리 회사의 조정 전·후 월 금액을 넣어 주세요")
    if pay_type == "그대로":
        before = after = other = None
    txt = lambda k, n=200: (f.get(k) or "").strip()[:n] or None      # noqa: E731
    vals = [emp_id, workplace, txt("project"), txt("client"), sd.isoformat(), ed.isoformat() if ed else None,
            ae.isoformat() if ae else None, reason, txt("reason_note", 500),
            *("1" if f.get(k) else None for k, _n in INSURANCES), pay_type,
            enc(before) if before else None, enc(after) if after else None, enc(other) if other else None, txt("note", 500)]
    cols = ["employee_id", "workplace", "project", "client", "start_date", "end_date", "actual_end", "reason", "reason_note",
            *(k for k, _n in INSURANCES), "pay_type", "our_before", "our_after", "other_pay", "note"]
    did = int(f["id"]) if str(f.get("id") or "").isdigit() else None
    if did:
        if not query_one("SELECT 1 x FROM dual_enrollments WHERE id=?", (did,)):
            raise DualError("기록을 찾을 수 없습니다")
        execute(f"UPDATE dual_enrollments SET {', '.join(c + '=?' for c in cols)} WHERE id=?", vals + [did])
    else:
        did = execute(f"INSERT INTO dual_enrollments({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    audit("hr", "이중가입 저장", f"emp#{emp_id}", workplace)       # 금액은 기록에 남기지 않음
    return did


def delete(did: int):
    r = query_one("SELECT employee_id, workplace FROM dual_enrollments WHERE id=?", (did,))
    if r:
        execute("DELETE FROM dual_enrollments WHERE id=?", (did,))
        audit("hr", "이중가입 삭제", f"emp#{r['employee_id']}", r["workplace"])


def toggle_check(did: int, key: str) -> bool:
    r = get(did)
    if not r or key not in {k for k, _n, _a in CHECK_START + CHECK_END}:
        return False
    checks = dict(r["checks"])
    if checks.get(key):
        checks.pop(key)
    else:
        checks[key] = today().isoformat()
    execute("UPDATE dual_enrollments SET checks=? WHERE id=?", (json.dumps(checks), did))
    return True


def apply_bases(did: int, which: str) -> int:
    """4대보험 기준 금액(건강·연금·고용 세 칸)을 조정 후(after) 또는 조정 전(before) 금액으로 바꿉니다. 담당자가 눌렀을 때만."""
    r = get(did)
    amount = r and r.get("our_after" if which == "after" else "our_before")
    if not r or r.get("pay_type") != "조정" or which not in ("after", "before") or not amount:
        raise DualError("조정 금액이 없는 기록입니다")
    if query_one("SELECT 1 x FROM employee_payroll WHERE employee_id=?", (r["employee_id"],)):
        execute("UPDATE employee_payroll SET insured_monthly=?, pension_monthly=?, employ_monthly=?, "
                "updated_at=datetime('now','localtime') WHERE employee_id=?", (amount, amount, amount, r["employee_id"]))
    else:
        execute("INSERT INTO employee_payroll(employee_id, insured_monthly, pension_monthly, employ_monthly, pay_items) "
                "VALUES (?,?,?,?,'{}')", (r["employee_id"], amount, amount, amount))
    audit("hr", "이중가입 → 보험 기준 금액 반영", f"emp#{r['employee_id']}", "조정 후" if which == "after" else "원복")
    return amount


def summary(all_rows: list[dict]) -> dict:
    on = [r for r in all_rows if r["state"] == "진행중"]
    return {"active": len({r["employee_id"] for r in on}), "ending": sum(1 for r in on if r["ending"]),
            "cleanup": sum(1 for r in all_rows if r["overdue"] or (r["state"] == "종료" and r["todo_left"])),
            "planned": sum(1 for r in all_rows if r["state"] == "예정"),
            "by_workplace": sorted(_count(on, "workplace").items(), key=lambda x: -x[1]),
            "by_project": sorted(_count(on, "project").items(), key=lambda x: -x[1])}


def _count(rs: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rs:
        out[r.get(key) or "(미입력)"] = out.get(r.get(key) or "(미입력)", 0) + 1
    return out


def in_year(emp_id: int, year: int) -> list[dict]:
    """그 해에 하루라도 이중가입이었던 기록 (연말정산: 두 곳 근로소득 합산 대상 안내)."""
    s, e = f"{year}-01-01", f"{year}-12-31"
    return [r for r in rows(employee_id=emp_id)
            if r["start_date"] <= e and (r.get("actual_end") or r.get("end_date") or "9999") >= s and r["start_date"] <= today().isoformat()]


def alerts() -> list[dict]:
    """인사 대시보드 알림 형식."""
    out = []
    for r in rows():
        emp = {"id": r["employee_id"], "name": r["name"]}
        link = f"/hr/dual?open={r['id']}&row={r['id']}"
        if r["overdue"]:
            out.append((("danger", "이중가입", emp, f"{r['workplace']} 종료 예정일({r['end_date']})이 지났습니다 — 종료 처리·"
                         + ("보수월액 원복 신고" if r["pay_type"] == "조정" else "상대 사업장 상실 확인") + " 필요", r["end_date"], link)))
        elif r["ending"]:
            out.append((("warning", "이중가입", emp, f"{r['workplace']} 이중가입 종료 예정 ({r['end_date']}) — 원복 준비"
                         + (" · 보수월액 원복 신고 필요" if r["pay_type"] == "조정" else ""), r["end_date"], link)))
        elif r["state"] == "종료" and r["todo_left"]:
            out.append((("info", "이중가입", emp, f"{r['workplace']} 종료 후 남은 처리: " + ", ".join(r["todo_left"][:3]), None, link)))
    return out
