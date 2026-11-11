"""경영관리 공통: 연도별 재직자, 부서 목록, 연봉 조회."""
from collections import defaultdict
from datetime import date

from app.common import to_date
from app.db import query
from app.modules.hr import repo as hr_repo


def depts() -> list[str]:
    return hr_repo.codes_with_used("DEPT")


def in_service(emp: dict, start: date, end: date) -> bool:
    """start~end 사이 하루라도 재직."""
    hd, rd = to_date(emp.get("hire_date")), to_date(emp.get("resign_date"))
    if hd and hd > end:
        return False
    if rd and rd < start:
        return False
    return True


def employees_in_year(year: int) -> list[dict]:
    s, e = date(year, 1, 1), date(year, 12, 31)
    return [r for r in query("SELECT * FROM employees ORDER BY department, emp_no, name") if in_service(r, s, e)]


def months_in_year(emp: dict, year: int) -> int:
    """그 해 재직 개월 수 (하루라도 다닌 달 포함)."""
    hd, rd = to_date(emp.get("hire_date")), to_date(emp.get("resign_date"))
    s = max(hd, date(year, 1, 1)) if hd else date(year, 1, 1)
    e = min(rd, date(year, 12, 31)) if rd else date(year, 12, 31)
    if e < s:
        return 0
    return max(0, min(12, (e.year - s.year) * 12 + e.month - s.month + 1))


def salary_map() -> dict[int, dict[int, int]]:
    """{직원: {연도: 연봉}} (복호화된 값)."""
    out = defaultdict(dict)
    for s in hr_repo.all_salaries():
        if s.get("annual_salary") is not None:
            out[s["employee_id"]][s["year"]] = int(s["annual_salary"])
    return out


def salary_for(hist: dict[int, int], year: int) -> tuple[int | None, int | None]:
    """그 해 연봉 (없으면 그 전 마지막 연봉)과 기준 연도."""
    if year in hist:
        return hist[year], year
    prev = [y for y in hist if y < year]
    return (hist[max(prev)], max(prev)) if prev else (None, None)


def round_to(v: float, unit: int) -> int:
    unit = max(1, int(unit or 1))
    return int((v + unit / 2) // unit * unit)
