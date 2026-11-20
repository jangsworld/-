"""개인별 연봉 현황: 입사 때부터의 연봉·증감액·증감율, 한 화면 입력, 과거 연봉 엑셀(CSV) 일괄 올리기."""
import csv
import io

from app.common import to_date, today
from app.db import audit, batch, get_codes, query, query_one
from app.modules.hr import repo, services

MIN_YEAR, MAX_AMOUNT = 1990, 10**11


class SalaryError(ValueError):
    pass


def _years(emp: dict, have: dict) -> list[int]:
    hd = to_date(emp.get("hire_date"))
    start = min([hd.year if hd else today().year] + list(have))
    rd = to_date(emp.get("resign_date"))
    end = max([min(today().year, rd.year) if rd else today().year] + list(have))
    return list(range(max(start, MIN_YEAR), end + 1))


def view(emp_id: int) -> dict | None:
    emp = repo.get_employee(emp_id)
    if not emp:
        return None
    have = {s["year"]: s for s in repo.employee_salaries(emp_id)}
    steps: dict = {}
    for st in repo.salary_steps(emp_id):
        steps.setdefault(st["year"], []).append(st)
    years = _years(emp, have)
    rows, prev = [], None

    def col(y, amt, name, s, when="", step=False):
        nonlocal prev
        diff = (amt - prev) if (amt and prev) else None
        d = to_date(when)
        rows.append({"year": y, "amount": amt, "diff": diff, "rate": (diff / prev * 100) if diff is not None and prev else None,
                     "reason": (s or {}).get("reason") or "", "id": (s or {}).get("id"), "name": name, "step": step,
                     "since": f"{d.month}/{d.day}~" if d else "", "label": f"{y}년" + (f" {d.month}/{d.day}~" if d else "")})
        if amt:
            prev = amt

    for y in years:
        s = have.get(y)
        ys = steps.get(y, []) if s else []
        for st in ys:           # 그 해 마지막 금액 이전에 적용되던 금액들
            col(y, st["annual_salary"], f"st_{st['id']}", st, st["effective_date"], True)
        last_when = ""
        if ys:
            d = (s.get("effective_date") or "")
            last_when = max(d if d[:4] == str(y) else f"{y}-01-01", ys[-1]["effective_date"])
        col(y, s["annual_salary"] if s else None, f"y_{y}", s, last_when)
    filled = [r for r in rows if r["amount"]]
    summary = None
    if len(filled) >= 2:
        first, last = filled[0], filled[-1]
        n = last["year"] - first["year"]
        summary = {"first": first, "last": last, "total": last["amount"] - first["amount"],
                   "total_rate": (last["amount"] - first["amount"]) / first["amount"] * 100,
                   "cagr": ((last["amount"] / first["amount"]) ** (1 / n) - 1) * 100 if n > 0 else None}
    # 비교선: 회사 평균 인상률(연도별), 같은 직급(지금 직급 기준) 평균 연봉
    trend = {t["year"]: t for t in services.salary_trend()}
    rank_avg: dict = {}
    if emp.get("rank"):
        for s in repo.all_salaries():
            if s.get("rank") == emp["rank"]:
                rank_avg.setdefault(s["year"], []).append(s["annual_salary"])
    for r in rows:
        r["co_rate"] = None if r["step"] else (trend.get(r["year"]) or {}).get("avg_rate")
        v = None if r["step"] else rank_avg.get(r["year"])
        r["rank_avg"] = round(sum(v) / len(v)) if v and len(v) >= 2 else None      # 1명뿐이면 본인 값이라 표시하지 않음
    # 발령 표시 (그 해에 있었던 승진·전보 등)
    marks: dict = {}
    for a in repo.list_appointments(emp_id):
        if a.get("status") == "예정" or a["appt_type"] in ("입사", "퇴사"):
            continue
        y = (a.get("appt_date") or "")[:4]
        if y.isdigit():
            marks.setdefault(int(y), []).append(f"{a['appt_type']}{' ' + a['to_value'] if a.get('to_value') else ''}")
    for r in rows:
        r["marks"] = [] if r["step"] else marks.get(r["year"], [])
    # 이전·다음 직원 (임직원 현황·개인별 연봉 목록과 같은 순서: 입사일·사번, 재직자)
    t = today().isoformat()
    order = [e["id"] for e in query("SELECT id, resign_date FROM employees ORDER BY hire_date, emp_no, id")
             if not e.get("resign_date") or e["resign_date"] >= t or e["id"] == emp_id]
    i = order.index(emp_id) if emp_id in order else -1
    return {"e": emp, "rows": rows, "summary": summary, "has_steps": bool(steps), "reasons": get_codes("SALARY_REASON"),
            "prev_id": order[i - 1] if i > 0 else None, "next_id": order[i + 1] if 0 <= i < len(order) - 1 else None}


def _reason(year: int, emp: dict, amount: int, prev: int | None) -> str:
    codes = get_codes("SALARY_REASON")
    hd = to_date(emp.get("hire_date"))
    want = "신규" if (hd and hd.year == year) or prev is None else ("동결" if prev == amount else "정기인상")
    return want if want in codes else (codes[0] if codes else "정기인상")


def save_years(emp_id: int, values: dict[int, int | None], overwrite: bool = True) -> dict:
    """연도별 금액 저장. None = 그 해 기록 삭제. overwrite=False 면 이미 있는 값은 건드리지 않음."""
    emp = repo.get_employee(emp_id)
    if not emp:
        raise SalaryError("직원을 찾을 수 없습니다")
    have = {s["year"]: s for s in repo.employee_salaries(emp_id)}
    added = changed = removed = kept = 0
    with batch():
        prev = None
        for y in sorted(set(values) | set(have)):
            old = have.get(y)
            if y not in values:
                prev = old["annual_salary"] if old else prev
                continue
            amt = values[y]
            if amt is None:
                if old and overwrite:
                    repo.delete_salary(old["id"])
                    removed += 1
                elif old:
                    prev = old["annual_salary"]
                continue
            if old and int(old["annual_salary"]) == amt:
                prev = amt
                continue
            if old and not overwrite:
                kept += 1
                prev = old["annual_salary"]
                continue
            repo.upsert_salary(emp_id, y, amt, (old or {}).get("effective_date"),
                               (old or {}).get("reason") or _reason(y, emp, amt, prev), (old or {}).get("note") or "")
            changed += 1 if old else 0
            added += 0 if old else 1
            prev = amt
    if added or changed or removed:
        audit("hr", "연봉 이력 저장", f"emp#{emp_id}", f"새로 {added} · 고침 {changed} · 삭제 {removed}")   # 금액은 기록하지 않음
    return {"added": added, "changed": changed, "removed": removed, "kept": kept}


def save_steps(emp_id: int, values: dict[int, int | None]) -> dict:
    """같은 해의 앞선 금액 고치기. None = 그 변경 삭제."""
    mine = {s["id"]: s for s in repo.salary_steps(emp_id)}
    changed = removed = 0
    with batch():
        for sid, amt in values.items():
            old = mine.get(sid)
            if not old:
                continue
            if amt is None:
                repo.delete_salary_step(sid)
                removed += 1
            elif amt != old["annual_salary"]:
                repo.update_salary_step(sid, amt)
                changed += 1
        repo.promote_salary_steps(emp_id)
    if changed or removed:
        audit("hr", "연중 연봉 변경 저장", f"emp#{emp_id}", f"고침 {changed} · 삭제 {removed}")
    return {"changed": changed, "removed": removed}


def add_change(emp_id: int, when: str, amount: int | None, reason: str = "", overwrite: bool = False) -> str:
    """적용일·금액으로 연봉 변경 한 건을 넣는다. 그 해에 이미 금액이 있으면 날짜 순서에 맞춰 나눠 저장.

    돌려주는 값: added(새로 넣음) / changed(같은 적용일의 금액을 고침) / kept(같은 적용일이 있어 그대로 둠) / same(같은 값)"""
    emp = repo.get_employee(emp_id)
    d = to_date(when)
    if not emp:
        raise SalaryError("직원을 찾을 수 없습니다")
    if not d or not MIN_YEAR <= d.year <= today().year + 1:
        raise SalaryError(f"적용일을 확인해 주세요: {str(when)[:20]}")
    if not amount:
        raise SalaryError("변경 후 연봉을 넣어 주세요")
    reason = reason if reason in get_codes("SALARY_REASON") else ""
    iso, y = d.isoformat(), d.year
    final = next((s for s in repo.employee_salaries(emp_id) if s["year"] == y), None)
    steps = [s for s in repo.salary_steps(emp_id) if s["year"] == y]
    with batch():
        if not final:
            repo.upsert_salary(emp_id, y, amount, iso, reason or _reason(y, emp, amount, None), "")
            res = "added"
        else:
            fd = final.get("effective_date") or ""
            fd = max([fd if fd[:4] == str(y) else f"{y}-01-01"] + [s["effective_date"] for s in steps])
            same = next((s for s in steps if s["effective_date"] == iso), None)
            if iso == fd or same:
                old = int(final["annual_salary"]) if iso == fd else same["annual_salary"]
                if old == amount:
                    return "same"
                if not overwrite:
                    return "kept"
                if iso == fd:
                    repo.upsert_salary(emp_id, y, amount, fd, final.get("reason") or "정기인상", final.get("note") or "")
                else:
                    repo.update_salary_step(same["id"], amount)
                res = "changed"
            elif iso > fd:        # 더 늦은 변경 → 지금까지의 금액은 '앞선 금액'으로 내리고, 새 금액이 그 해 연봉
                repo.add_salary_step(emp_id, fd, int(final["annual_salary"]), final.get("reason") or "")
                repo.upsert_salary(emp_id, y, amount, iso, reason or "정기인상", final.get("note") or "")
                res = "added"
            else:
                repo.add_salary_step(emp_id, iso, amount, reason)
                res = "added"
    audit("hr", "연중 연봉 변경 등록", f"emp#{emp_id}", f"{iso} ({res})")          # 금액은 기록하지 않음
    return res


def changes_template_csv() -> str:
    """적용일 양식: 한 줄에 한 번의 변경. 이미 등록된 연중 변경을 채워서 내려줍니다."""
    emps = {e["id"]: e for e in query("SELECT id, emp_no, name FROM employees")}
    finals = {(s["employee_id"], s["year"]): s for s in repo.all_salaries()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["사번", "성명", "적용일", "연봉", "사유"])
    seen = set()
    for st in repo.salary_steps():
        e = emps.get(st["employee_id"])
        if not e:
            continue
        w.writerow([e.get("emp_no") or "", e["name"], st["effective_date"], st["annual_salary"], st.get("reason") or ""])
        seen.add((st["employee_id"], st["year"]))
    for emp_id, y in sorted(seen):
        f, e = finals.get((emp_id, y)), emps[emp_id]
        if f:
            d = f.get("effective_date") or ""
            w.writerow([e.get("emp_no") or "", e["name"], d if d[:4] == str(y) else f"{y}-01-01", f["annual_salary"], f.get("reason") or ""])
    if not seen:
        w.writerow(["(예) 2020-001", "홍길동", "2025-01-01", "40000000", "정기인상"])
        w.writerow(["(예) 2020-001", "홍길동", "2025-07-01", "44000000", "승진"])
    return buf.getvalue()


def _import_changes(rows: list[list[str]], head: list[str], emp_of, overwrite: bool) -> str:
    """'사번, 성명, 적용일, 연봉, 사유' — 한 줄에 한 번의 변경."""
    i_date = head.index("적용일")
    i_amt = next((i for i, h in enumerate(head) if h in ("연봉", "금액", "연봉액")), None)
    i_reason = next((i for i, h in enumerate(head) if h == "사유"), None)
    if i_amt is None:
        return "첫 줄에 '연봉' 칸이 필요합니다"
    plan, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        cell = lambda i: r[i].strip() if i is not None and i < len(r) else ""      # noqa: E731
        if cell(0).startswith("(예)"):
            continue
        emp_id, err = emp_of(r)
        if err:
            errors.append(f"{n}번째 줄: {err}")
            continue
        d = to_date(cell(i_date).replace(".", "-").replace("/", "-"))
        try:
            amt = parse_amount(cell(i_amt))
        except SalaryError as ex:
            errors.append(f"{n}번째 줄: {ex}")
            continue
        if not d or not MIN_YEAR <= d.year <= today().year + 1 or not amt:
            errors.append(f"{n}번째 줄: 적용일(예: 2025-07-01)과 연봉을 확인해 주세요")
            continue
        plan.append((d.isoformat(), emp_id, amt, cell(i_reason)))
    if errors:
        return "저장하지 않았습니다 — " + " / ".join(errors[:5]) + (f" 외 {len(errors) - 5}건" if len(errors) > 5 else "")
    tot = {"added": 0, "changed": 0, "kept": 0, "same": 0}
    with batch():
        for iso, emp_id, amt, reason in sorted(plan):        # 적용일 순서대로
            tot[add_change(emp_id, iso, amt, reason, overwrite)] += 1
    audit("hr", "연봉 변경(적용일) 일괄 올리기", f"{len(plan)}줄", f"새로 {tot['added']} · 고침 {tot['changed']} · 그대로 둠 {tot['kept']}")
    return (f"{len(plan)}줄: 새로 넣음 {tot['added']}건 · 고침 {tot['changed']}건"
            + (f" · 이미 값이 있어 그대로 둔 것 {tot['kept']}건 ('이미 있는 값도 바꾸기'에 체크하면 바뀝니다)" if tot["kept"] else ""))


def parse_amount(v) -> int | None:
    s = str(v or "").replace(",", "").replace("원", "").strip()
    if not s or s == "-":
        return None
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit() or not 0 < int(s) <= MAX_AMOUNT:
        raise SalaryError(f"금액을 확인해 주세요: {str(v)[:20]}")
    return int(s)


def template_csv() -> str:
    """일괄 올리기 양식: 재직자 × 연도. 이미 등록된 금액은 채워서 내려줍니다."""
    t = today()
    emps = [e for e in query("SELECT id, emp_no, name, department, hire_date, resign_date FROM employees ORDER BY department, name")
            if not e.get("resign_date") or e["resign_date"] >= t.isoformat()]
    have: dict = {}
    for s in repo.all_salaries():
        have.setdefault(s["employee_id"], {})[s["year"]] = s["annual_salary"]
    first = min([to_date(e["hire_date"]).year for e in emps if to_date(e.get("hire_date"))] + [t.year])
    years = list(range(max(first, MIN_YEAR), t.year + 1))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["사번", "성명", "부서", "입사일"] + [str(y) for y in years])
    for e in emps:
        hy = to_date(e["hire_date"]).year if to_date(e.get("hire_date")) else years[0]
        w.writerow([e.get("emp_no") or "", e["name"], e.get("department") or "", e.get("hire_date") or ""]
                   + [(have.get(e["id"], {}).get(y) or "") if y >= hy else "" for y in years])
    return buf.getvalue()


def import_csv(text: str, overwrite: bool) -> str:
    """'사번, 성명, …, 2018, 2019, …' 표를 읽어 저장. 한 줄이라도 형식이 틀리면 아무것도 저장하지 않습니다."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        return "내용이 없습니다. 첫 줄에 사번·성명·연도(예: 2018)를 적어 주세요"
    head = [h.strip().replace("년도", "").replace("년", "") for h in rows[0]]
    ycols = {i: int(h) for i, h in enumerate(head) if h.isdigit() and MIN_YEAR <= int(h) <= today().year + 1}
    long_form = "적용일" in head
    if not ycols and not long_form:
        return "첫 줄에 연도 칸(예: 2018, 2019)이 없습니다"
    i_no = next((i for i, h in enumerate(head) if h in ("사번", "사원번호")), None)
    i_name = next((i for i, h in enumerate(head) if h in ("성명", "이름", "사원명")), None)
    if i_no is None and i_name is None:
        return "첫 줄에 '사번' 또는 '성명' 칸이 필요합니다"
    emps = query("SELECT id, emp_no, name FROM employees")
    by_no = {e["emp_no"]: e["id"] for e in emps if e.get("emp_no")}
    by_name: dict = {}
    for e in emps:
        by_name.setdefault(e["name"], []).append(e["id"])

    def emp_of(r):
        cell = lambda i: r[i].strip() if i is not None and i < len(r) else ""      # noqa: E731
        emp_id = by_no.get(cell(i_no)) if cell(i_no) else None
        if not emp_id and cell(i_name):
            ids = by_name.get(cell(i_name), [])
            if len(ids) > 1:
                return None, "같은 이름이 여러 명이라 사번이 필요합니다"
            emp_id = ids[0] if ids else None
        return (emp_id, "") if emp_id else (None, "직원을 찾을 수 없습니다")

    if long_form:
        return _import_changes(rows, head, emp_of, overwrite)
    plan, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        cell = lambda i: r[i].strip() if i is not None and i < len(r) else ""      # noqa: E731
        emp_id, err = emp_of(r)
        if err:
            errors.append(f"{n}번째 줄: {err}")
            continue
        try:
            vals = {y: parse_amount(cell(i)) for i, y in ycols.items()}
        except SalaryError as ex:
            errors.append(f"{n}번째 줄: {ex}")
            continue
        vals = {y: v for y, v in vals.items() if v is not None}       # 빈 칸은 건드리지 않음(삭제하지 않음)
        if vals:
            plan.append((emp_id, vals))
    if errors:
        return "저장하지 않았습니다 — " + " / ".join(errors[:5]) + (f" 외 {len(errors) - 5}건" if len(errors) > 5 else "")
    tot = {"added": 0, "changed": 0, "kept": 0}
    with batch():
        for emp_id, vals in plan:
            r = save_years(emp_id, vals, overwrite)
            for k in tot:
                tot[k] += r[k]
    audit("hr", "과거 연봉 일괄 올리기", f"{len(plan)}명", f"새로 {tot['added']} · 고침 {tot['changed']} · 그대로 둠 {tot['kept']}")
    return (f"{len(plan)}명: 새로 넣음 {tot['added']}건 · 고침 {tot['changed']}건"
            + (f" · 이미 값이 있어 그대로 둔 것 {tot['kept']}건 ('이미 있는 값도 바꾸기'에 체크하면 바뀝니다)" if tot["kept"] else ""))
