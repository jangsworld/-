"""인건비 총괄: 연봉(근무 개월 비례) + 인센티브 + 4대보험 회사부담 + 퇴직급여 충당 = 회사 총 인건비(추정)."""
from collections import defaultdict

from app.db import get_settings
from app.modules.mgmt import fin, incentive
from app.modules.mgmt.common import employees_in_year, months_in_year, salary_for, salary_map
from app.modules.mgmt.constants import DEFAULT_EMPLOYER_RATE, DEFAULT_RETIRE_RATE


def rates() -> dict:
    s = get_settings()

    def f(k, d):
        try:
            return float(s.get(k) or d)
        except ValueError:
            return d
    return {"employer": f("labor_employer_rate", DEFAULT_EMPLOYER_RATE), "retire": f("labor_retire_rate", DEFAULT_RETIRE_RATE)}


def overview(year: int, sal=None, inc_by_emp=None, inc_dept=None) -> dict:
    rt = rates()
    sal = sal if sal is not None else salary_map()
    inc_by_emp = inc_by_emp if inc_by_emp is not None else incentive.employee_totals(year)
    inc_dept = inc_dept if inc_dept is not None else incentive.dept_unassigned_paid(year)
    people, missing = [], []
    for e in employees_in_year(year):
        months = months_in_year(e, year)
        annual, base_year = salary_for(sal.get(e["id"], {}), year)
        if annual is None:
            missing.append(e)
        s = round((annual or 0) * months / 12)
        inc = inc_by_emp.get(e["id"], 0)
        gross = s + inc
        people.append({"id": e["id"], "name": e["name"], "department": e.get("department") or "미지정", "rank": e.get("rank"),
                       "months": months, "annual": annual, "base_year": base_year, "salary": s, "incentive": inc,
                       "employer": round(gross * rt["employer"] / 100), "retire": round(gross * rt["retire"] / 100)})
    for p in people:
        p["total"] = p["salary"] + p["incentive"] + p["employer"] + p["retire"]
    dept = defaultdict(lambda: {"fte": 0.0, "count": 0, "salary": 0, "incentive": 0, "employer": 0, "retire": 0})
    for p in people:
        d = dept[p["department"]]
        d["count"] += 1
        d["fte"] += p["months"] / 12
        for k in ("salary", "incentive", "employer", "retire"):
            d[k] += p[k]
    for dn, amt in inc_dept.items():           # 부서로만 지급한 인센티브
        d = dept[dn or "미지정"]
        d["incentive"] += amt
        d["employer"] += round(amt * rt["employer"] / 100)
        d["retire"] += round(amt * rt["retire"] / 100)
    rows = []
    for dn, d in dept.items():
        d["total"] = d["salary"] + d["incentive"] + d["employer"] + d["retire"]
        rows.append({"department": dn, **d, "per_head": d["total"] / d["fte"] if d["fte"] else None})
    rows.sort(key=lambda r: -r["total"])
    tot = {k: sum(r[k] for r in rows) for k in ("salary", "incentive", "employer", "retire", "total")}
    tot["fte"] = sum(r["fte"] for r in rows)
    tot["count"] = len(people)
    for r in rows:
        r["share"] = r["total"] / tot["total"] * 100 if tot["total"] else None
    f = fin.summary(year)
    rev, op = f["by"]["매출액"]["cur"], f["by"]["영업손익"]["cur"]
    return {
        "year": year, "rates": rt, "people": sorted(people, key=lambda p: (p["department"], -p["total"])),
        "rows": rows, "total": tot, "missing": missing, "fin": f,
        "per_head": tot["total"] / tot["fte"] if tot["fte"] else None,
        "labor_ratio": tot["total"] / rev * 100 if rev else None,
        "rev_per_head": rev / tot["fte"] if (rev and tot["fte"]) else None,
        "op_per_head": op / tot["fte"] if (op is not None and tot["fte"]) else None,
        "op_before_labor": (op + tot["total"]) if op is not None else None,
    }


def trend(years: list[int]) -> list[dict]:
    sal = salary_map()
    out = []
    for y in years:
        o = overview(y, sal=sal)
        out.append({"year": y, "salary": o["total"]["salary"], "incentive": o["total"]["incentive"],
                    "extra": o["total"]["employer"] + o["total"]["retire"], "total": o["total"]["total"],
                    "fte": o["total"]["fte"], "per_head": o["per_head"], "labor_ratio": o["labor_ratio"]})
    return out
