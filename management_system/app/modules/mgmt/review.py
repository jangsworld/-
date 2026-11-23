"""인사평가·연봉조정 (2026-11 개편: 평가표 없이 기준값으로 고정인상률 안을 자동 산출).

흐름: ① 기준값(물가상승률·최저임금·손익) → ② 추천 범위(하한·추천·상한)와 고정인상률 안 자동 산출·비교
→ ③ 직원별: 고정인상률 + 등급 가산 + 추가(%p) = 최종. 최저임금 미달은 자동으로 기준선까지
→ ④ 고른 고정안 대비 조정 결과 비교 후 확정 → 인건비·연봉에 반영.
(예전에는 ③에서 '최종 %'를 직접 넣었다. 그 값은 convert_overrides 가 '추가(%p)'로 한 번 바꿔 둔다.)
"""
import csv
import io
import json
import math
from collections import Counter, defaultdict
from datetime import date

from app.common import to_date
from app.db import audit, batch, execute, get_codes, get_settings, query, query_one
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
                "afford_ratio": "인상 비용 상한(영업이익 대비 %)", "budget_rate": "목표 총액 기준 인상률(%)",
                "guide": "등급 분포 권장(%)", "band": "직급별 연봉 범위(만원)", "memo": "메모"}
TEXT_INPUTS = ("memo", "guide", "band")
INPUT_KEYS = tuple(INPUT_LABELS)
INPUT_DEFAULTS = {"share_ratio": 10.0, "share_cap": 3.0, "pay_ratio": 30.0, "afford_ratio": 20.0}
EXTRA_LIMIT = 10.0              # 추가 조정 한 사람당 ±%p
# 추천 범위: 영업이익률이 이만큼이면 성과 점수 만점, 영업이익 증가율이 이만큼이면 만점 (둘의 평균)
MARGIN_FULL, GROWTH_FULL = 10.0, 20.0
# 등급 분포 권장(%) — 등급이 기본(S~D)일 때만 기본값을 씀. 권장보다 이만큼(%p) 넘게 많으면 표시
DEFAULT_GUIDE = {"S": 10.0, "A": 20.0, "B": 40.0, "C": 20.0, "D": 10.0}
GUIDE_TOL = 5.0
# 연봉 밴드: 직급별 범위를 따로 안 넣으면 같은 직급 새 연봉 중간값 ±이 % (3명 이상일 때만)
BAND_TOL, BAND_MIN_PEOPLE = 20.0, 3


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
        if k in TEXT_INPUTS:
            t = str(form.get(k) or "").strip()[:500]
            if k == "guide" and t:
                parse_guide(t, [g for g, _ in parse_grades(form.get("grades") or ", ".join(f"{g}:{v:+g}" for g, v in c["grades"]))])
            if k == "band" and t:
                parse_band(t)
            inputs[k] = t
            continue
        v = str(form.get(k) or "").strip().replace(",", "").replace("%", "")
        if v and _num(v) is None:
            raise ReviewError(f"{INPUT_LABELS[k]}은(는) 숫자로 넣어 주세요")
        inputs[k] = v
    if "base_rate" in form and "budget_rate" not in form:
        inputs["budget_rate"] = ""                  # 초안을 새로 정하면 예산 중립 목표도 새 초안으로
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
        extra = (it.get("extra_rate") or 0.0) if use_overrides else 0.0
        override = it.get("final_rate") if use_overrides else None
        final = override if override is not None else (base or 0.0) + add + extra
        new = round_to(base_sal * (1 + final / 100), unit) if base_sal else None
        min_fix = False
        if new and min_annual and new < min_annual and e.get("employment_type") != "단시간(파트타임)":
            new = -(-min_annual // unit) * unit                  # 최저임금 기준선까지 자동으로 올림 (단위 올림)
            final = (new / base_sal - 1) * 100
            min_fix = True
        rows.append({
            "employee": e, "grade": grade, "add_rate": add, "add_custom": it.get("add_rate") is not None,
            "extra_rate": it.get("extra_rate") if use_overrides else None, "fixed_rate": base or 0.0,
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
        "afford_ratio": opt("afford_ratio"),
        "margin": (op / f["by"]["매출액"]["cur"] * 100) if (op is not None and f["by"]["매출액"]["cur"]) else None,
        "mult": 1 + (rt["employer"] + rt["retire"]) / 100, "rates": rt, "fin": f,
        "total_base": compute(c, base_rate=0, use_overrides=False, with_grades=False)["total_base"],
    }


def plans(c: dict, b: dict | None = None) -> list[dict]:
    """고정인상률 안 자동 산출. 자료가 없으면 rate=None 과 채울 값 안내(why)."""
    b = b or basis(c)
    cpi = b["cpi"]
    rc = recommend(b)
    out = []
    if rc["ready"]:
        out += [{"key": "low", "name": "추천 하한", "rate": rc["low"], "formula": rc["low_why"], "why": None},
                {"key": "rec", "name": "추천안", "rate": rc["rec"], "formula": rc["rec_why"], "why": None},
                {"key": "high", "name": "추천 상한", "rate": rc["high"], "formula": rc["high_why"], "why": None}]
    out += [
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


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def recommend(b: dict) -> dict:
    """작년 손익·물가상승률·최저시급으로 고정인상률의 하한·추천·상한을 냅니다 (근거 글과 함께).

    - 하한: 물가상승률과 최저임금 인상률 중 낮은 값 = 실질임금을 지키는 최소선. 적자면 0%(동결)까지 내려갑니다.
    - 성과 점수(0~1): 영업이익률(10%면 만점)과 영업이익 증가율(20%면 만점)의 평균. 적자면 0.
    - 추천: 물가상승률 + 성과 점수 × 성과 가산 상한. 적자면 하한 기준의 절반.
    - 상한: 물가·최저임금 인상률 중 높은 값 + 성과 가산 상한 전부(가장 후하게 줄 때). 손익 자료가 없으면 가산 없이.
      단, 회사 실부담이 작년 영업이익의 '인상 비용 상한' %를 넘지 않게.
    """
    cpi, mw = b.get("cpi"), b.get("mw_rate")
    known = [v for v in (cpi, mw) if v is not None]
    if not known:
        return {"ready": False, "why": "물가상승률이나 다음 해 최저시급을 넣으면 추천 범위가 계산됩니다"}
    lo_base, hi_base = min(known), max(known)
    op, margin, growth = b.get("op"), b.get("margin"), b.get("op_growth_rate")
    share_cap = b.get("share_cap", INPUT_DEFAULTS["share_cap"])
    afford = b.get("afford_ratio", INPUT_DEFAULTS["afford_ratio"])
    loss = op is not None and op <= 0
    parts = []
    if margin is not None:
        parts.append(_clamp(margin / MARGIN_FULL, 0, 1))
    if growth is not None:
        parts.append(_clamp(growth / GROWTH_FULL, 0, 1))
    score = 0.0 if loss else (sum(parts) / len(parts) if parts else None)
    bonus = (score or 0.0) * share_cap
    anchor = cpi if cpi is not None else lo_base
    if loss:
        low, rec, high = 0.0, lo_base / 2, lo_base
    else:
        low, rec, high = lo_base, anchor + bonus, hi_base + (share_cap if score is not None else 0.0)
    aff, notes = None, []
    if op is not None and op > 0 and b.get("total_base"):
        aff = op * afford / 100 / (b["total_base"] * b.get("mult", 1)) * 100
        if aff < high:
            notes.append(f"회사 실부담이 영업이익의 {afford:g}%를 넘지 않도록 상한을 {aff:.1f}%로 낮췄습니다")
            high = max(low, aff)
        if aff < low:
            notes.append(f"하한({low:.1f}%)만 줘도 실부담이 영업이익의 {afford:g}%를 넘습니다. 인상 폭을 신중히 검토하세요")
    if loss:
        notes.append("작년 영업이익이 적자입니다. 동결(0%)부터 물가 수준까지를 범위로 잡았습니다")
    if score is None:
        notes.append("손익 자료가 없어 성과 가산 없이 물가·최저임금만으로 계산했습니다 (fund에서 읽거나 직접 입력하면 반영)")
    rec = _clamp(rec, low, high)
    r1 = lambda v: math.floor(v * 10 + 0.5 + 1e-9) / 10      # noqa: E731  (0.05 는 올림)
    low, rec, high = r1(low), r1(rec), r1(high)
    src = " · ".join(x for x in (f"물가 {cpi:g}%" if cpi is not None else "", f"최저임금 {mw:.1f}%" if mw is not None else "") if x)
    return {
        "ready": True, "low": low, "rec": rec, "high": high, "score": score, "bonus": bonus, "aff": aff, "loss": loss,
        "margin": margin, "growth": growth, "notes": notes,
        "low_why": "적자 → 동결까지" if loss else f"물가·최저임금 인상률 중 낮은 값 ({src})",
        "rec_why": (f"하한의 절반 (적자)" if loss else
                    f"물가 {anchor:g}% + 성과 가산 {bonus:.1f}%p (성과 점수 {round((score or 0) * 100)}점 × 상한 {share_cap:g}%p)"),
        "high_why": ("물가·최저임금 중 낮은 값 (적자)" if loss else
                     f"물가·최저임금 중 높은 값 + 성과 가산 상한 {share_cap:g}%p (가장 후하게)" if score is not None else "물가·최저임금 중 높은 값") +
                    (f", 지불능력 {aff:.1f}%로 제한" if aff is not None and aff < hi_base + share_cap and not loss else ""),
    }


def convert_overrides() -> int:
    """예전 방식 '최종 % 직접'을 '추가(%p)'로 바꿉니다 (반영완료한 해는 그대로). 한 번만."""
    if get_settings().get("review_extra_v1") == "1":
        return 0
    from app.db import set_setting
    n = 0
    with batch():
        for c in cycles():
            if c["status"] == "반영완료" or c["base_rate"] is None:
                continue
            gmap = {g: v for g, v in c["grades"]}
            for it in query("SELECT * FROM review_items WHERE cycle_id=? AND final_rate IS NOT NULL", (c["id"],)):
                add = it["add_rate"] if it["add_rate"] is not None else gmap.get(it["grade"] or "", 0.0)
                extra = round(it["final_rate"] - c["base_rate"] - add, 2)
                execute("UPDATE review_items SET extra_rate=?, final_rate=NULL WHERE id=?", (extra or None, it["id"]))
                n += 1
        set_setting("review_extra_v1", "1")
        if n:
            audit("mgmt", "인사평가 최종 직접 → 추가 조정 변환", "", f"{n}건")
    return n


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
    rc = recommend(b)
    if rc["ready"]:
        g = compute(c, base_rate=rc["rec"], use_overrides=False, with_grades=False)
        rc["labor_ratio_after"] = (labor_now + g["increase"] * mult) / rev * 100 if rev else None
        rc["real_cost"] = g["increase"] * mult
    return {"rows": out, "plans": pl, "basis": b, "fin": f, "mult": mult, "rates": rt, "labor_now": labor_now, "rec": rc,
            "labor_ratio_now": labor_now / rev * 100 if rev else None}


# ── 확정 전 점검: 등급 분포 · 예산 중립 · 연봉 밴드 ─────────────────────────
def parse_guide(text: str, grades: list[str]) -> dict[str, float]:
    """'S:10, A:20, B:40, C:20, D:10' → {'S': 10.0, ...}. 합계 100%, 있는 등급만."""
    out = {}
    for part in (text or "").replace("\n", ",").split(","):
        if not part.strip():
            continue
        if ":" not in part:
            raise ReviewError(f"등급 분포는 'S:10, A:20' 처럼 적어 주세요: {part.strip()}")
        g, v = (x.strip() for x in part.split(":", 1))
        if g not in grades:
            raise ReviewError(f"등급 분포에 없는 등급이 있습니다: {g}")
        try:
            out[g] = float(v.replace("%", "") or 0)
        except ValueError as ex:
            raise ReviewError(f"등급 분포 비율을 읽지 못했습니다: {part.strip()}") from ex
        if out[g] < 0:
            raise ReviewError("등급 분포 비율은 0 이상으로 넣어 주세요")
    if out and abs(sum(out.values()) - 100) > 0.5:
        raise ReviewError(f"등급 분포 비율의 합이 100%가 되어야 합니다 (지금 {sum(out.values()):g}%)")
    return out


def parse_band(text: str) -> dict[str, tuple[int, int]]:
    """'사원:3000~3800, 대리:3600~4600' (만원) → {'사원': (30000000, 38000000), ...}"""
    out = {}
    for part in (text or "").replace("\n", ",").split(","):
        if not part.strip():
            continue
        try:
            rank, rng = (x.strip() for x in part.split(":", 1))
            lo, hi = (float(x.strip().replace("만원", "").replace(",", "")) for x in rng.replace("-", "~").split("~", 1))
        except ValueError as ex:
            raise ReviewError(f"직급별 연봉 범위는 '사원:3000~3800' 처럼 만원 단위로 적어 주세요: {part.strip()}") from ex
        if not rank or lo <= 0 or hi < lo:
            raise ReviewError(f"직급별 연봉 범위를 확인해 주세요 (낮은 값 ~ 높은 값): {part.strip()}")
        out[rank] = (int(lo * 10000), int(hi * 10000))
    return out


def guide_targets(c: dict) -> dict[str, float]:
    grades = [g for g, _ in c["grades"]]
    try:
        g = parse_guide(c["inputs"].get("guide") or "", grades)
    except ReviewError:
        g = {}
    if g:
        return g
    return dict(DEFAULT_GUIDE) if grades == list(DEFAULT_GUIDE) else {}


def grade_guide(c: dict, res: dict) -> dict:
    """등급 분포: 권장 비율·인원과 실제를 나란히. 후한 등급(가산 +)이 권장보다 많으면 경고."""
    tg = guide_targets(c)
    n = res["graded"]
    dist = res["grade_dist"]
    rows = []
    for g, v in c["grades"]:
        cnt = dist.get(g, 0)
        pct = cnt / n * 100 if n else 0.0
        t = tg.get(g)
        tc = round(n * t / 100) if (t is not None and n) else None
        rows.append({"grade": g, "add": v, "count": cnt, "pct": pct, "target": t, "target_count": tc,
                     "over": bool(tc is not None and pct > t + GUIDE_TOL and cnt > tc)})
    pos = [r for r in rows if r["add"] > 0]
    good_pct = sum(r["pct"] for r in pos)
    good_target = sum(r["target"] for r in pos) if pos and all(r["target"] is not None for r in pos) else None
    return {"rows": rows, "graded": n, "count": res["count"], "has_target": bool(tg), "good_pct": good_pct,
            "good_target": good_target, "good_names": "·".join(r["grade"] for r in pos),
            "lenient": bool(good_target is not None and n and good_pct > good_target + GUIDE_TOL),
            "tol": GUIDE_TOL, "text": ", ".join(f"{g}:{t:g}" for g, t in tg.items())}


def budget_target_rate(c: dict, rc: dict | None = None) -> float | None:
    """예산 중립의 목표: 맞춤 때 저장해 둔 값, 없으면 지금 고른 고정인상률(초안).
    ②에서 고정인상률을 새로 정하면 저장해 둔 목표는 지워져 새 초안이 목표가 됩니다."""
    v = _num(c["inputs"].get("budget_rate"))
    return v if v is not None else c["base_rate"]


def neutral(c: dict, target_rate: float | None) -> dict | None:
    """등급·추가로 늘어난(줄어든) 총액만큼 고정인상률을 조정해, 총 인상액을 '모두 목표 % 일괄'과 같게(넘지 않게) 맞춤.
    고정은 0.1% 단위, 목표를 넘지 않는 가장 높은 값."""
    if c["base_rate"] is None or target_rate is None:
        return None
    now = compute(c)
    if not now["total_base"]:
        return None                                  # 기준 연봉이 하나도 없으면 맞출 총액이 없음
    target = compute(c, base_rate=target_rate, use_overrides=False, with_grades=False)["increase"]
    cur = now["increase"]
    cache = {}

    def cost(i):
        if i not in cache:
            cache[i] = compute(c, base_rate=i / 10)["increase"]
        return cache[i]
    out = {"target_rate": target_rate, "target": target, "current": cur, "gap": cur - target, "rate": None, "after": None}
    lo, hi = -100, 300
    if cost(lo) > target:
        return out
    if cost(hi) <= target:
        lo = hi
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cost(mid) <= target:
            lo = mid
        else:
            hi = mid
    out.update(rate=lo / 10, after=cost(lo))
    return out


def set_neutral(c: dict, target_rate: float, by: str = "") -> dict:
    c = _fresh(c)
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
    if not -10 <= target_rate <= 30:
        raise ReviewError("목표 인상률은 -10~30% 사이로 넣어 주세요")
    nt = neutral(c, target_rate)
    if not nt:
        raise ReviewError("고정인상률을 먼저 정하고, 기준 연봉이 있어야 맞출 수 있습니다")
    if nt["rate"] is None:
        raise ReviewError("등급·추가 조정이 너무 커서 고정인상률을 -10%까지 낮춰도 목표 총액에 맞출 수 없습니다")
    save_setup(c, {"base_rate": f"{nt['rate']:g}", "budget_rate": f"{target_rate:g}"}, by)
    audit("mgmt", "인사평가 예산 중립 맞춤", str(c["year"]), f"목표 {target_rate:g}% 일괄 → 고정 {nt['rate']:g}%")
    return nt


def salary_bands(c: dict, res: dict) -> dict:
    """직급별 연봉 밴드 점검 (새 연봉 기준). 넣어 둔 범위가 있으면 그것, 없으면 같은 직급 중간값 ±BAND_TOL%."""
    try:
        manual = parse_band(c["inputs"].get("band") or "")
    except ReviewError:
        manual = {}
    by = defaultdict(list)
    for r in res["rows"]:
        e = r["employee"]
        if r["new_salary"] and e.get("employment_type") != "단시간(파트타임)":
            by[(e.get("rank") or "").strip() or "직급 없음"].append(r)
    order = {n: i for i, n in enumerate(get_codes("RANK"))}
    ranks, flags = [], {}
    for rank in sorted(by, key=lambda k: (order.get(k, 999), k)):
        rs = by[rank]
        vals = sorted(r["new_salary"] for r in rs)
        k = len(vals)
        med = vals[k // 2] if k % 2 else (vals[k // 2 - 1] + vals[k // 2]) / 2
        if rank in manual:
            lo, hi, src = manual[rank][0], manual[rank][1], "넣어 둔 범위"
        elif k >= BAND_MIN_PEOPLE:
            lo, hi, src = med * (1 - BAND_TOL / 100), med * (1 + BAND_TOL / 100), f"중간값 ±{BAND_TOL:g}%"
        else:
            ranks.append({"rank": rank, "count": k, "median": med, "lo": None, "hi": None,
                          "src": f"{BAND_MIN_PEOPLE}명 미만이라 점검 안 함", "low": 0, "high": 0})
            continue
        low = high = 0
        for r in rs:
            v = r["new_salary"]
            if v < lo or v > hi:
                side = "low" if v < lo else "high"
                low += side == "low"
                high += side == "high"
                flags[r["employee"]["id"]] = {"side": side, "lo": lo, "hi": hi, "rank": rank, "src": src,
                                             "gap": (lo - v) if side == "low" else (v - hi)}
        ranks.append({"rank": rank, "count": k, "median": med, "lo": lo, "hi": hi, "src": src, "low": low, "high": high})
    out = [dict(r, band=flags[r["employee"]["id"]]) for r in res["rows"] if r["employee"]["id"] in flags]
    return {"ranks": ranks, "flags": flags, "out": out, "tol": BAND_TOL, "manual": bool(manual)}


def checks(c: dict, res: dict, rc: dict | None = None) -> dict:
    """③ 아래 점검 상자에 쓰는 세 가지."""
    t = budget_target_rate(c, rc)
    return {"guide": grade_guide(c, res), "neutral": neutral(c, t), "bands": salary_bands(c, res),
            "target_rate": t, "target_saved": _num(c["inputs"].get("budget_rate")) is not None}


def save_items(c: dict, rows: list[dict], by: str = "") -> int:
    c = _fresh(c)
    if c["locked"]:
        raise ReviewError("확정된 평가는 바꿀 수 없습니다. 먼저 '다시 열기'를 하세요.")
    grades = {g for g, _ in c["grades"]}
    allowed = {e["id"] for e in members(c)}
    cur = items(c)

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
                extra = num(r.get("extra_rate")) if "extra_rate" in r else (cur.get(eid) or {}).get("extra_rate")
            except ValueError as ex:
                raise ReviewError("가산·추가·최종 인상률은 숫자로 넣어 주세요") from ex
            if extra is not None and abs(extra) > EXTRA_LIMIT:
                raise ReviewError(f"추가 조정은 ±{EXTRA_LIMIT:g}%p 안에서 넣어 주세요")
            execute("INSERT INTO review_items(cycle_id, employee_id, grade, add_rate, final_rate, extra_rate, comment, evaluator, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime')) ON CONFLICT(cycle_id, employee_id) DO UPDATE SET "
                    "grade=excluded.grade, add_rate=excluded.add_rate, final_rate=excluded.final_rate, extra_rate=excluded.extra_rate, "
                    "comment=excluded.comment, evaluator=excluded.evaluator, updated_at=excluded.updated_at",
                    (c["id"], eid, g or None, add, final, extra or None, enc((r.get("comment") or "").strip() or None),
                     (r.get("evaluator") or "").strip() or None))
            n += 1
        if c["status"] == "준비" and n:
            execute("UPDATE review_cycles SET status='평가중' WHERE id=? AND status='준비'", (c["id"],))
    audit("mgmt", "인사평가 입력", str(c["year"]), f"{n}명")
    return n


def bulk_set(c: dict, emp_ids: list[int], grade: str | None = None, final: str | None = None, by: str = "",
             extra: str | None = None) -> int:
    """여러 명에게 한꺼번에. grade·extra·final 이 None 이면 그 칸은 그대로 두고, "" 이면 비웁니다. 메모 등 다른 칸은 건드리지 않습니다."""
    if grade is None and final is None and extra is None:
        raise ReviewError("바꿀 내용을 고르세요 (등급 또는 추가 %p)")
    cur = items(c)
    keep = lambda v: "" if v is None else str(v)       # noqa: E731
    rows = []
    for eid in emp_ids:
        it = cur.get(eid) or {}
        rows.append({"employee_id": eid, "grade": keep(it.get("grade")) if grade is None else grade,
                     "final_rate": keep(it.get("final_rate")) if final is None else final,
                     "extra_rate": keep(it.get("extra_rate")) if extra is None else extra,
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
