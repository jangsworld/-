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
    years = _years(emp, have)
    rows, prev = [], None
    for y in years:
        s = have.get(y)
        amt = s["annual_salary"] if s else None
        diff = (amt - prev) if (amt and prev) else None
        rows.append({"year": y, "amount": amt, "diff": diff, "rate": (diff / prev * 100) if diff is not None and prev else None,
                     "reason": (s or {}).get("reason") or "", "id": (s or {}).get("id")})
        if amt:
            prev = amt
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
        r["co_rate"] = (trend.get(r["year"]) or {}).get("avg_rate")
        v = rank_avg.get(r["year"])
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
        r["marks"] = marks.get(r["year"], [])
    # 이전·다음 직원 (부서·이름 순, 재직자)
    t = today().isoformat()
    order = [e["id"] for e in query("SELECT id, resign_date FROM employees ORDER BY department, name, id")
             if not e.get("resign_date") or e["resign_date"] >= t or e["id"] == emp_id]
    i = order.index(emp_id) if emp_id in order else -1
    return {"e": emp, "rows": rows, "summary": summary,
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
    if not ycols:
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
    plan, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        cell = lambda i: r[i].strip() if i is not None and i < len(r) else ""      # noqa: E731
        emp_id = by_no.get(cell(i_no)) if cell(i_no) else None
        if not emp_id and cell(i_name):
            ids = by_name.get(cell(i_name), [])
            if len(ids) > 1:
                errors.append(f"{n}번째 줄: 같은 이름이 여러 명이라 사번이 필요합니다")
                continue
            emp_id = ids[0] if ids else None
        if not emp_id:
            errors.append(f"{n}번째 줄: 직원을 찾을 수 없습니다")
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
