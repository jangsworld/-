"""인사평가·연봉조정.

흐름: ① 성과연도 손익(fund)으로 고정인상률 안(5·6·7% 등) 비교 → ② 부서장 평가 등급 입력(관리부가 입력하거나 CSV 올리기)
→ ③ 직원별 최종 인상률 = 고정 + 등급 가산(개별 조정 가능) → ④ 확정 → 연봉현황에 반영.
"""
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import date

from app.common import to_date
from app.db import audit, batch, execute, get_settings, query, query_one
from app.modules.hr import repo as hr_repo
from app.modules.mgmt import fin, labor
from app.modules.mgmt.common import in_service, round_to, salary_for, salary_map
from app.modules.mgmt.constants import DEFAULT_GRADES, DEFAULT_SCENARIOS
from app.security.crypto import enc

LOCKED = ("확정", "반영완료")


class ReviewError(ValueError):
    pass


def _cycle_row(r: dict | None) -> dict | None:
    if not r:
        return None
    r["scenarios"] = json.loads(r["scenarios"] or "[]") or list(DEFAULT_SCENARIOS)
    r["grades"] = json.loads(r["grades"] or "[]") or [list(g) for g in DEFAULT_GRADES]
    r["inputs"] = r["inputs"] if isinstance(r["inputs"], dict) else {}
    r["locked"] = r["status"] in LOCKED
    return r


def cycles() -> list[dict]:
    return [_cycle_row(r) for r in query("SELECT * FROM review_cycles ORDER BY year DESC")]


def get(year: int) -> dict | None:
    return _cycle_row(query_one("SELECT * FROM review_cycles WHERE year=?", (int(year),)))


def create(year: int, by: str = "") -> dict:
    if get(year):
        return get(year)
    ay = int(year) + 1
    execute("INSERT INTO review_cycles(year, apply_year, eval_start, eval_end, scenarios, grades, inputs, effective_date) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(year), ay, f"{ay}-05-01", f"{ay}-05-20", json.dumps(DEFAULT_SCENARIOS), json.dumps(DEFAULT_GRADES),
             enc({}), f"{ay}-01-01"))
    audit("mgmt", "인사평가 만들기", str(year), by)
    return get(year)


def parse_grades(text: str) -> list[list]:
    """'S:+2, A:+1, B:0' → [['S', 2.0], ...]"""
    out = []
    for part in (text or "").replace("\n", ",").split(","):
        if ":" not in part:
            continue
        g, v = part.split(":", 1)
        g = g.strip()
        try:
            out.append([g, float(v.strip().replace("%p", "").replace("%", "") or 0)])
        except ValueError as ex:
            raise ReviewError(f"등급 가산을 읽지 못했습니다: {part.strip()}") from ex
    if not out or len({g for g, _ in out}) != len(out):
        raise ReviewError("등급은 'S:+2, A:+1, B:0' 처럼 겹치지 않게 적어 주세요")
    return out


def parse_rates(text: str) -> list[float]:
    try:
        rs = sorted({round(float(x.strip().replace("%", "")), 2) for x in (text or "").split(",") if x.strip()})
    except ValueError as ex:
        raise ReviewError("비교할 인상률은 '5, 6, 7' 처럼 숫자로 적어 주세요") from ex
    if not rs or len(rs) > 6 or any(r < -10 or r > 30 for r in rs):
        raise ReviewError("비교할 인상률은 1~6개, -10~30% 사이로 적어 주세요")
    return rs


def save_setup(c: dict, form: dict, by: str = ""):
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
    def keep(k):    # 폼에 없는 칸은 지금 값 유지 (예: 비교표에서 '이 안으로 정하기'만 누른 경우)
        return form.get(k) if k in form else c.get(k)
    s, e, eff = to_date(keep("eval_start")), to_date(keep("eval_end")), to_date(keep("effective_date"))
    if s and e and e < s:
        raise ReviewError("평가 종료일이 시작일보다 빠릅니다")
    inputs = dict(c["inputs"])
    for k in ("cpi", "min_wage_next", "industry", "memo"):
        if k in form:
            inputs[k] = str(form.get(k) or "").strip()
    base = str(keep("base_rate") if keep("base_rate") is not None else "").strip()
    try:
        base_rate = float(base) if base else None
        unit = int(form.get("round_unit") or c["round_unit"] or 10000)
    except ValueError as ex:
        raise ReviewError("고정인상률·반올림 단위는 숫자로 넣어 주세요") from ex
    if base_rate is not None and not -10 <= base_rate <= 30:
        raise ReviewError("고정인상률은 -10~30% 사이로 넣어 주세요")
    execute("UPDATE review_cycles SET eval_start=?, eval_end=?, scenarios=?, grades=?, inputs=?, base_rate=?, round_unit=?, "
            "effective_date=? WHERE id=?",
            (s.isoformat() if s else None, e.isoformat() if e else None,
             json.dumps(parse_rates(form.get("scenarios") or ",".join(str(x) for x in c["scenarios"]))),
             json.dumps(parse_grades(form.get("grades") or ", ".join(f"{g}:{v:+g}" for g, v in c["grades"]))),
             enc(inputs), base_rate, unit, eff.isoformat() if eff else c["effective_date"], c["id"]))
    audit("mgmt", "인사평가 기준 저장", str(c["year"]), f"고정 {base_rate}%")


def set_status(c: dict, status: str, by: str = ""):
    cur = c["status"]
    allowed = {("준비", "평가중"), ("평가중", "확정"), ("준비", "확정"), ("확정", "평가중")}
    if (cur, status) not in allowed:
        raise ReviewError(f"'{cur}'에서 '{status}'(으)로 바꿀 수 없습니다")
    if status == "확정":
        if c["base_rate"] is None:
            raise ReviewError("고정인상률을 먼저 정해 주세요")
        # 기준 연봉이 없는 직원은 확정해도 연봉현황 반영에서 빠집니다 (화면 '확인 필요'에 표시)
        execute("UPDATE review_cycles SET status=?, decided_at=datetime('now','localtime'), decided_by=? WHERE id=?",
                (status, by, c["id"]))
    else:
        execute("UPDATE review_cycles SET status=? WHERE id=?", (status, c["id"]))
    audit("mgmt", f"인사평가 {status}", str(c["year"]), by)


def members(c: dict) -> list[dict]:
    """평가 대상: 성과연도 말까지 입사, 평가 시작 전 퇴사자 제외."""
    start = to_date(c.get("eval_start")) or date(c["apply_year"], 5, 1)
    out = []
    for e in query("SELECT * FROM employees ORDER BY department, emp_no, name"):
        if not in_service(e, start, date(9999, 12, 31)):
            continue
        hd = to_date(e.get("hire_date"))
        if hd and hd > date(c["year"], 12, 31):
            continue
        if e.get("employment_type") == "일용직":
            continue
        out.append(e)
    return out


def items(c: dict) -> dict[int, dict]:
    return {r["employee_id"]: r for r in query("SELECT * FROM review_items WHERE cycle_id=?", (c["id"],))}


def compute(c: dict, base_rate: float | None = None, use_overrides: bool = True, with_grades: bool = True) -> dict:
    base = c["base_rate"] if base_rate is None else base_rate
    gmap = {g: v for g, v in c["grades"]}
    its = items(c)
    sal = salary_map()
    s = get_settings()
    try:
        min_wage = float(str(c["inputs"].get("min_wage_next") or s.get("min_wage_hourly") or 0).replace(",", ""))
        std = float(s.get("monthly_std_hours") or 209)
    except ValueError:
        min_wage, std = 0.0, 209.0
    rows = []
    for e in members(c):
        it = its.get(e["id"], {})
        hist = sal.get(e["id"], {})
        base_sal, base_year = salary_for(hist, c["year"])
        grade = it.get("grade") or ""
        add = it["add_rate"] if it.get("add_rate") is not None else gmap.get(grade, 0.0)
        if not with_grades:
            add = 0.0
        override = it.get("final_rate") if use_overrides else None
        final = override if override is not None else (base or 0.0) + add
        new = round_to(base_sal * (1 + final / 100), c["round_unit"] or 10000) if base_sal else None
        rows.append({
            "employee": e, "grade": grade, "add_rate": add, "add_custom": it.get("add_rate") is not None,
            "final_rate": final, "override": override, "comment": it.get("comment") or "", "evaluator": it.get("evaluator") or "",
            "base_salary": base_sal, "base_year": base_year, "new_salary": new,
            "raise": (new - base_sal) if (new and base_sal) else None,
            "already": hist.get(c["apply_year"]),
            "below_min": bool(new and min_wage and new / 12 / std < min_wage),
        })
    have = [r for r in rows if r["base_salary"]]
    tb, tn = sum(r["base_salary"] for r in have), sum(r["new_salary"] for r in have)
    dept = defaultdict(lambda: {"count": 0, "base": 0, "new": 0, "graded": 0})
    for r in rows:
        d = dept[r["employee"].get("department") or "미지정"]
        d["count"] += 1
        d["graded"] += 1 if r["grade"] else 0
        if r["base_salary"]:
            d["base"] += r["base_salary"]
            d["new"] += r["new_salary"]
    return {
        "rows": rows, "total_base": tb, "total_new": tn, "increase": tn - tb,
        "increase_rate": (tn - tb) / tb * 100 if tb else None,
        "avg_rate": sum(r["final_rate"] for r in have) / len(have) if have else None,
        "graded": sum(1 for r in rows if r["grade"]), "count": len(rows),
        "missing_salary": [r for r in rows if not r["base_salary"]],
        "below_min": [r for r in rows if r["below_min"]],
        "already": sum(1 for r in rows if r["already"]),
        "grade_dist": Counter(r["grade"] or "미평가" for r in rows),
        "dept": sorted(({"department": k, **v, "rate": (v["new"] - v["base"]) / v["base"] * 100 if v["base"] else None}
                        for k, v in dept.items()), key=lambda x: x["department"]),
    }


def scenarios(c: dict) -> dict:
    """고정인상률 안별 비교 (평가 가산 포함·제외), 회사 실부담과 손익 대비."""
    rt = labor.rates()
    mult = 1 + (rt["employer"] + rt["retire"]) / 100
    f = fin.summary(c["year"])
    rev, op = f["by"]["매출액"]["cur"], f["by"]["영업손익"]["cur"]
    op_prev = f["by"]["영업손익"]["prev"]
    labor_now = labor.overview(c["year"])["total"]["total"]
    rates = sorted(set(c["scenarios"] + ([c["base_rate"]] if c["base_rate"] is not None else [])))
    out = []
    for r in rates:
        g = compute(c, base_rate=r, use_overrides=False)
        n = compute(c, base_rate=r, use_overrides=False, with_grades=False)
        real = g["increase"] * mult
        out.append({
            "rate": r, "chosen": r == c["base_rate"], "increase_fixed": n["increase"], "increase": g["increase"],
            "avg_rate": g["avg_rate"], "total_new": g["total_new"], "real_cost": real,
            "op_share": real / op * 100 if op and op > 0 else None,
            "op_growth_share": real / (op - op_prev) * 100 if (op is not None and op_prev is not None and op > op_prev) else None,
            "labor_ratio_after": (labor_now + real) / rev * 100 if rev else None,
            "below_min": len(g["below_min"]),
        })
    return {"rows": out, "fin": f, "mult": mult, "rates": rt, "labor_now": labor_now,
            "labor_ratio_now": labor_now / rev * 100 if rev else None}


def save_items(c: dict, rows: list[dict], by: str = "") -> int:
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
    grades = {g for g, _ in c["grades"]}
    allowed = {e["id"] for e in members(c)}

    def num(v):
        v = str(v if v is not None else "").strip().replace("%", "")
        return float(v) if v not in ("", "None") else None
    n = 0
    with batch():
        for r in rows:
            eid = int(r["employee_id"])
            if eid not in allowed:
                continue
            g = (r.get("grade") or "").strip()
            if g and g not in grades:
                raise ReviewError(f"없는 등급입니다: {g}")
            try:
                add, final = num(r.get("add_rate")), num(r.get("final_rate"))
            except ValueError as ex:
                raise ReviewError("가산·최종 인상률은 숫자로 넣어 주세요") from ex
            execute("INSERT INTO review_items(cycle_id, employee_id, grade, add_rate, final_rate, comment, evaluator, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,datetime('now','localtime')) ON CONFLICT(cycle_id, employee_id) DO UPDATE SET "
                    "grade=excluded.grade, add_rate=excluded.add_rate, final_rate=excluded.final_rate, comment=excluded.comment, "
                    "evaluator=excluded.evaluator, updated_at=excluded.updated_at",
                    (c["id"], eid, g or None, add, final, enc((r.get("comment") or "").strip() or None),
                     (r.get("evaluator") or "").strip() or None))
            n += 1
        if c["status"] == "준비" and n:
            execute("UPDATE review_cycles SET status='평가중' WHERE id=?", (c["id"],))
    audit("mgmt", "인사평가 입력", str(c["year"]), f"{n}명")
    return n


def apply(c: dict, by: str = "") -> int:
    if c["status"] != "확정":
        raise ReviewError("확정한 뒤에 연봉현황에 반영할 수 있습니다")
    rows = [r for r in compute(c)["rows"] if r["new_salary"]]
    with batch():
        for r in rows:
            note = f"{c['year']}년 인사평가 {r['grade'] or '미평가'} · 최종 {r['final_rate']:.2f}%"
            hr_repo.upsert_salary(r["employee"]["id"], c["apply_year"], r["new_salary"], c["effective_date"], "정기인상", note)
        execute("UPDATE review_cycles SET status='반영완료', applied_at=datetime('now','localtime'), applied_by=? WHERE id=?",
                (by, c["id"]))
    audit("mgmt", "인사평가 연봉 반영", str(c["year"]), f"{len(rows)}명 → {c['apply_year']}년 연봉")
    return len(rows)


# ── 부서장 평가표 CSV (연봉 금액 없음) ──────────────────────────────────
CSV_HEAD = ["부서", "사번", "성명", "직급", "입사일", "등급", "의견"]


def sheet_rows(c: dict, department: str = "") -> list[list]:
    its = items(c)
    out = [CSV_HEAD]
    for e in members(c):
        if department and (e.get("department") or "") != department:
            continue
        it = its.get(e["id"], {})
        out.append([e.get("department") or "", e.get("emp_no") or "", e["name"], e.get("rank") or "",
                    e.get("hire_date") or "", it.get("grade") or "", it.get("comment") or ""])
    return out


def import_sheet(c: dict, text: str, by: str = "") -> tuple[int, list[str]]:
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다")
    rd = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not rd.fieldnames or "성명" not in rd.fieldnames or "등급" not in rd.fieldnames:
        raise ReviewError("평가표 형식이 아닙니다. 이 화면에서 내려받은 평가표를 써 주세요.")
    mem = members(c)
    by_no = {str(e.get("emp_no") or ""): e for e in mem if e.get("emp_no")}
    by_name = defaultdict(list)
    for e in mem:
        by_name[(e["name"], e.get("department") or "")].append(e)
    its = items(c)
    rows, errs = [], []
    for i, r in enumerate(rd, 2):
        e = by_no.get((r.get("사번") or "").strip())
        if not e:
            cand = by_name.get(((r.get("성명") or "").strip(), (r.get("부서") or "").strip()), [])
            e = cand[0] if len(cand) == 1 else None
        if not e:
            if (r.get("등급") or "").strip():
                errs.append(f"{i}행 {r.get('성명') or ''}: 평가 대상에서 찾지 못했습니다")
            continue
        it = its.get(e["id"], {})
        rows.append({"employee_id": e["id"], "grade": (r.get("등급") or "").strip().upper(),
                     "add_rate": it.get("add_rate"), "final_rate": it.get("final_rate"),
                     "comment": (r.get("의견") or "").strip(), "evaluator": it.get("evaluator")})
    n = save_items(c, rows, by) if rows else 0
    return n, errs
