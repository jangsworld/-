"""인사평가·연봉조정 (2026-11 개편: 평가표 없이 기준값으로 고정인상률 안을 자동 산출).

흐름: ① 기준값(물가상승률·최저임금·손익) → ② 고정인상률 안 자동 산출·비교(물가보전·최저임금연동·성과공유·지불능력·직접)
→ ③ 고른 안을 전원에 먼저 적용, 직원별 최종 %를 직접 조정(등급 가산은 선택), 최저임금 미달은 자동으로 기준선까지
→ ④ 고른 고정안 대비 조정 결과 비교 후 확정 → 인건비·연봉에 반영.
"""
import csv
import io
import json
import math
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
STATUS_LABEL = {"준비": "준비", "평가중": "조정 중", "확정": "확정", "반영완료": "반영완료"}
# 기준값 입력칸 (review_cycles.inputs, 암호화 저장)
INPUT_LABELS = {"cpi": "물가상승률(%)", "min_wage_cur": "올해 최저시급(원)", "min_wage_next": "다음 해 최저시급(원)",
                "share_ratio": "성과공유 나눔비율(%)", "share_cap": "성과공유 가산 상한(%p)", "pay_ratio": "지불능력 한도(%)",
                "memo": "메모"}
INPUT_KEYS = tuple(INPUT_LABELS)
INPUT_DEFAULTS = {"share_ratio": 10.0, "share_cap": 3.0, "pay_ratio": 30.0}


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
    if len(rs) > 6 or any(r < -10 or r > 30 for r in rs):
        raise ReviewError("직접 비교할 인상률은 6개까지, -10~30% 사이로 적어 주세요")
    return rs


def _fresh(c: dict) -> dict:
    """저장 직전에 DB 에서 다시 읽은 조정. 화면을 연 뒤(요청을 받는 사이) 확정·반영됐을 수 있음."""
    now = get(c["year"])
    if not now:
        raise ReviewError("그 해 연봉조정을 찾을 수 없습니다")
    return now


def save_setup(c: dict, form: dict, by: str = ""):
    c = _fresh(c)
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
    def keep(k):    # 폼에 없는 칸은 지금 값 유지 (예: 비교표에서 '이 안으로 정하기'만 누른 경우)
        return form.get(k) if k in form else c.get(k)
    s, e, eff = to_date(keep("eval_start")), to_date(keep("eval_end")), to_date(keep("effective_date"))
    if s and e and e < s:
        raise ReviewError("평가 종료일이 시작일보다 빠릅니다")
    inputs = dict(c["inputs"])
    for k in INPUT_KEYS:
        if k not in form:
            continue
        if k == "memo":
            inputs[k] = str(form.get(k) or "").strip()[:500]
            continue
        v = str(form.get(k) or "").strip().replace(",", "").replace("%", "")
        if v and _num(v) is None:
            raise ReviewError(f"{INPUT_LABELS[k]}은(는) 숫자로 넣어 주세요")
        inputs[k] = v
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
             json.dumps(parse_rates(form["scenarios"] if "scenarios" in form else ",".join(str(x) for x in c["scenarios"]))),
             json.dumps(parse_grades(form.get("grades") or ", ".join(f"{g}:{v:+g}" for g, v in c["grades"]))),
             enc(inputs), base_rate, unit, eff.isoformat() if eff else c["effective_date"], c["id"]))
    audit("mgmt", "인사평가 기준 저장", str(c["year"]), f"고정 {base_rate}%")


def set_status(c: dict, status: str, by: str = ""):
    c = _fresh(c)
    cur = c["status"]
    allowed = {("준비", "평가중"), ("평가중", "확정"), ("준비", "확정"), ("확정", "평가중")}
    if (cur, status) not in allowed:
        raise ReviewError(f"'{cur}'에서 '{status}'(으)로 바꿀 수 없습니다")
    if status == "확정":
        if c["base_rate"] is None:
            raise ReviewError("고정인상률을 먼저 정해 주세요")
        # 기준 연봉이 없는 직원은 확정해도 연봉현황 반영에서 빠집니다 (화면 '확인 필요'에 표시)
        execute("UPDATE review_cycles SET status=?, decided_at=datetime('now','localtime'), decided_by=? WHERE id=? AND status=?",
                (status, by, c["id"], cur))
    else:
        execute("UPDATE review_cycles SET status=? WHERE id=? AND status=?", (status, c["id"], cur))
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
    unit = c["round_unit"] or 10000
    min_annual = math.ceil(min_wage * std * 12) if min_wage else 0
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
        new = round_to(base_sal * (1 + final / 100), unit) if base_sal else None
        min_fix = False
        if new and min_annual and new < min_annual and e.get("employment_type") != "단시간(파트타임)":
            new = -(-min_annual // unit) * unit                  # 최저임금 기준선까지 자동으로 올림 (단위 올림)
            final = (new / base_sal - 1) * 100
            min_fix = True
        rows.append({
            "employee": e, "grade": grade, "add_rate": add, "add_custom": it.get("add_rate") is not None,
            "final_rate": final, "override": override, "comment": it.get("comment") or "", "evaluator": it.get("evaluator") or "",
            "base_salary": base_sal, "base_year": base_year, "new_salary": new,
            "raise": (new - base_sal) if (new and base_sal) else None,
            "already": hist.get(c["apply_year"]),
            "below_min": bool(new and min_wage and new / 12 / std < min_wage and e.get("employment_type") != "단시간(파트타임)"),
            "min_fix": min_fix,
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
        "min_fix": [r for r in rows if r["min_fix"]], "min_wage": min_wage, "min_annual": min_annual,
        "already": sum(1 for r in rows if r["already"]),
        "grade_dist": Counter(r["grade"] or "미평가" for r in rows),
        "dept": sorted(({"department": k, **v, "rate": (v["new"] - v["base"]) / v["base"] * 100 if v["base"] else None}
                        for k, v in dept.items()), key=lambda x: x["department"]),
    }


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def basis(c: dict) -> dict:
    """안 산출에 쓰는 기준값: 직접 넣은 값 + 손익(fund·직접 입력) + 기준 연봉 합계."""
    inp = c["inputs"]
    rt = labor.rates()
    f = fin.summary(c["year"])
    op, op_prev = f["by"]["영업손익"]["cur"], f["by"]["영업손익"]["prev"]
    mw_cur = _num(inp.get("min_wage_cur")) or _num(get_settings().get("min_wage_hourly"))
    mw_next = _num(inp.get("min_wage_next"))

    def opt(k):
        v = _num(inp.get(k))
        return INPUT_DEFAULTS[k] if v is None else v
    return {
        "cpi": _num(inp.get("cpi")), "mw_cur": mw_cur, "mw_next": mw_next,
        "mw_rate": (mw_next / mw_cur - 1) * 100 if (mw_cur and mw_next) else None,
        "op": op, "op_prev": op_prev, "rev": f["by"]["매출액"]["cur"],
        "op_growth": (op - op_prev) if (op is not None and op_prev is not None) else None,
        "op_growth_rate": ((op - op_prev) / abs(op_prev) * 100) if (op is not None and op_prev) else None,
        "share_ratio": opt("share_ratio"), "share_cap": opt("share_cap"), "pay_ratio": opt("pay_ratio"),
        "mult": 1 + (rt["employer"] + rt["retire"]) / 100, "rates": rt, "fin": f,
        "total_base": compute(c, base_rate=0, use_overrides=False, with_grades=False)["total_base"],
    }


def plans(c: dict, b: dict | None = None) -> list[dict]:
    """고정인상률 안 자동 산출. 자료가 없으면 rate=None 과 채울 값 안내(why)."""
    b = b or basis(c)
    cpi = b["cpi"]
    out = [
        {"key": "cpi", "name": "물가보전안", "rate": round(cpi, 1) if cpi is not None else None,
         "formula": "물가상승률 그대로 (실질임금 유지)", "why": None if cpi is not None else "물가상승률을 넣으면 계산됩니다"},
        {"key": "minwage", "name": "최저임금연동안", "rate": round(b["mw_rate"], 1) if b["mw_rate"] is not None else None,
         "formula": "다음 해 최저시급 ÷ 올해 최저시급 − 1",
         "why": None if b["mw_rate"] is not None else "다음 해 최저시급을 넣으면 계산됩니다"},
    ]
    if cpi is None:
        share, why = None, "물가상승률을 넣으면 계산됩니다"
    elif b["op_growth_rate"] is None:
        share, why = None, "올해·전년 영업이익(fund 읽기 또는 직접 입력)이 있어야 계산됩니다"
    else:
        share, why = round(cpi + min(max(b["op_growth_rate"], 0) * b["share_ratio"] / 100, b["share_cap"]), 1), None
    out.append({"key": "share", "name": "성과공유안", "rate": share, "why": why,
                "formula": f"물가상승률 + 영업이익 증가율 × {b['share_ratio']:g}% (가산 최대 {b['share_cap']:g}%p, 이익이 줄면 가산 0)"})
    if b["op_growth"] is None or not b["total_base"]:
        pay, why = None, "올해·전년 영업이익과 기준 연봉이 있어야 계산됩니다"
    else:
        pay, why = round(max(b["op_growth"], 0) * b["pay_ratio"] / 100 / (b["total_base"] * b["mult"]) * 100, 1), None
    out.append({"key": "pay", "name": "지불능력안", "rate": pay, "why": why,
                "formula": f"늘어난 영업이익의 {b['pay_ratio']:g}% 안에서 줄 수 있는 최대 인상률 (4대보험·퇴직 회사 부담 포함)"})
    for r in c["scenarios"]:
        out.append({"key": "manual", "name": "직접입력안", "rate": float(r), "formula": "직접 넣은 비교안", "why": None})
    return out


def compare(c: dict) -> dict | None:
    """④ 확정 전 비교: 고른 고정인상률을 전원 똑같이 적용했을 때 vs 직원별 조정 후 실제 결과."""
    if c["base_rate"] is None:
        return None
    mult = 1 + sum(labor.rates().values()) / 100
    fx = compute(c, use_overrides=False, with_grades=False)
    fn = compute(c)
    fixed_by = {d["department"]: d for d in fx["dept"]}
    dept = [{"department": d["department"], "fixed": (fixed_by.get(d["department"]) or {}).get("rate"), "final": d["rate"]}
            for d in fn["dept"]]
    bins = [("0% 이하", None, 0.0001), ("0~2%", 0.0001, 2), ("2~4%", 2, 4), ("4~6%", 4, 6), ("6~8%", 6, 8), ("8% 이상", 8, None)]
    have = [r for r in fn["rows"] if r["base_salary"]]
    dist = [{"label": lb, "count": sum(1 for r in have if (lo is None or r["final_rate"] >= lo) and (hi is None or r["final_rate"] < hi))}
            for lb, lo, hi in bins]
    diff = [r for r in have if abs(r["final_rate"] - c["base_rate"]) > 0.005]

    def pack(x):
        return {"avg_rate": x["avg_rate"], "increase_rate": x["increase_rate"], "increase": x["increase"],
                "real_cost": x["increase"] * mult, "total_new": x["total_new"]}
    return {"fixed": pack(fx), "final": pack(fn), "dept": dept, "dist": dist, "changed": len(diff),
            "up": sum(1 for r in diff if r["final_rate"] > c["base_rate"]),
            "down": sum(1 for r in diff if r["final_rate"] < c["base_rate"])}


def scenarios(c: dict) -> dict:
    """고정인상률 안별 비교 (자동 산출 안 + 직접 넣은 안 + 고른 값), 회사 실부담과 손익 대비."""
    b = basis(c)
    pl = plans(c, b)
    rt, mult, f = b["rates"], b["mult"], b["fin"]
    rev, op, op_prev = b["rev"], b["op"], b["op_prev"]
    labor_now = labor.overview(c["year"])["total"]["total"]
    names = defaultdict(list)
    for p in pl:
        if p["rate"] is not None:
            names[p["rate"]].append(p["name"])
    if c["base_rate"] is not None and c["base_rate"] not in names:
        names[c["base_rate"]].append("고른 값")
    out = []
    for r in sorted(names):
        g = compute(c, base_rate=r, use_overrides=False)
        n = compute(c, base_rate=r, use_overrides=False, with_grades=False)
        real = g["increase"] * mult
        out.append({
            "rate": r, "names": names[r], "chosen": r == c["base_rate"], "increase_fixed": n["increase"], "increase": g["increase"],
            "avg_rate": g["avg_rate"], "total_new": g["total_new"], "real_cost": real,
            "op_share": real / op * 100 if op and op > 0 else None,
            "op_growth_share": real / (op - op_prev) * 100 if (op is not None and op_prev is not None and op > op_prev) else None,
            "labor_ratio_after": (labor_now + real) / rev * 100 if rev else None,
            "below_min": len(g["below_min"]),
        })
    return {"rows": out, "plans": pl, "basis": b, "fin": f, "mult": mult, "rates": rt, "labor_now": labor_now,
            "labor_ratio_now": labor_now / rev * 100 if rev else None}


def save_items(c: dict, rows: list[dict], by: str = "") -> int:
    c = _fresh(c)
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
    grades = {g for g, _ in c["grades"]}
    allowed = {e["id"] for e in members(c)}

    def num(v):
        v = str(v if v is not None else "").strip().replace("%", "")
        return float(v) if v not in ("", "None") else None
    n = 0
    with batch():
        # 묶음 안에서 먼저 쓰기 잠금을 잡고 상태를 다시 확인 → 확정·반영과 동시에 들어와도 순서가 보장됨
        execute("UPDATE review_cycles SET status=status WHERE id=?", (c["id"],))
        c = _fresh(c)
        if c["locked"]:
            raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
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
            execute("UPDATE review_cycles SET status='평가중' WHERE id=? AND status='준비'", (c["id"],))
    audit("mgmt", "인사평가 입력", str(c["year"]), f"{n}명")
    return n


def bulk_set(c: dict, emp_ids: list[int], grade: str | None = None, final: str | None = None, by: str = "") -> int:
    """여러 명에게 한꺼번에. grade·final 이 None 이면 그 칸은 그대로 두고, "" 이면 비웁니다. 메모 등 다른 칸은 건드리지 않습니다."""
    if grade is None and final is None:
        raise ReviewError("바꿀 내용을 고르세요 (등급 또는 최종 직접 %)")
    cur = items(c)
    keep = lambda v: "" if v is None else str(v)       # noqa: E731
    rows = []
    for eid in emp_ids:
        it = cur.get(eid) or {}
        rows.append({"employee_id": eid, "grade": keep(it.get("grade")) if grade is None else grade,
                     "final_rate": keep(it.get("final_rate")) if final is None else final,
                     "add_rate": keep(it.get("add_rate")), "comment": it.get("comment") or "", "evaluator": it.get("evaluator") or ""})
    return save_items(c, rows, by) if rows else 0


def apply(c: dict, by: str = "") -> int:
    c = _fresh(c)
    if c["status"] != "확정":
        raise ReviewError("확정한 뒤에 연봉현황에 반영할 수 있습니다")
    with batch():
        execute("UPDATE review_cycles SET status=status WHERE id=?", (c["id"],))     # 쓰기 잠금 뒤 다시 확인
        c = _fresh(c)
        if c["status"] != "확정":
            raise ReviewError("확정한 뒤에 연봉현황에 반영할 수 있습니다")
        rows = [r for r in compute(c)["rows"] if r["new_salary"]]
        for r in rows:
            note = f"{c['year']}년 인사평가 {r['grade'] or '미평가'} · 최종 {r['final_rate']:.2f}%"
            hr_repo.upsert_salary(r["employee"]["id"], c["apply_year"], r["new_salary"], c["effective_date"], "정기인상", note)
        execute("UPDATE review_cycles SET status='반영완료', applied_at=datetime('now','localtime'), applied_by=? "
                "WHERE id=? AND status='확정'", (by, c["id"]))
    audit("mgmt", "인사평가 연봉 반영", str(c["year"]), f"{len(rows)}명 → {c['apply_year']}년 연봉")
    return len(rows)


# ── 부서장 평가표 CSV (연봉 금액 없음) ──────────────────────────────────
CSV_HEAD = ["부서", "사번", "성명", "직급", "입사일", "등급", "의견"]


def sheet_rows(c: dict, department: str = "") -> list[list]:
    from app.db import dept_scope  # 상위 부서를 고르면 하위 부서 직원까지
    scope = dept_scope(department)
    its = items(c)
    out = [CSV_HEAD]
    for e in members(c):
        if department and (e.get("department") or "") not in scope:
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
