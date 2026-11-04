"""인사관리 비즈니스 로직: 문서 자동작성, 연봉 분석, 대시보드 집계, 관리 알림."""
import re
from datetime import timedelta
from collections import Counter, defaultdict

from app.common import (
    today, to_date, add_years, add_months, service_period, service_years_float,
    korean_amount, won, days_until,
)
from app.db import get_settings, query
from app.modules.hr import repo
from app.modules.hr.constants import (
    EMPLOYMENT_TO_CONTRACT, LEAVE_CATEGORIES, REDUCE_CATEGORIES,
)

_PH = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


# ════════════════════════════════════════════════════════════════════════
# 문서 자동작성
# ════════════════════════════════════════════════════════════════════════
def _kdate(v) -> str:
    d = to_date(v)
    return f"{d.year}년 {d.month:02d}월 {d.day:02d}일" if d else ""


def default_contract_params(emp: dict, category: str) -> dict:
    """임직원·구분에 따른 계약조건 기본값."""
    start = emp.get("hire_date") or today().isoformat()
    if category == "연봉계약":
        start = f"{today().year}-01-01"
        end = f"{today().year}-12-31"
    elif category in ("기간제", "단시간", "일용직"):
        sd = to_date(start)
        end = (add_years(sd, 1) - timedelta(days=1)).isoformat()
    else:
        end = ""
    weekly = 40 if category not in ("단시간",) else 20
    return {
        "start_date": start,
        "end_date": end,
        "probation": "3개월" if category == "정규직" else "없음",
        "work_days": "월~금 (주 5일)" if category != "단시간" else "월·수·금",
        "start_time": "09:00",
        "end_time": "18:00" if category != "단시간" else "13:00",
        "break_time": "12:00~13:00 (1시간)" if category != "단시간" else "없음",
        "weekly_hours": weekly,
        "annual_salary": emp.get("annual_salary") or "",
        "hourly_wage": get_settings().get("min_wage_hourly", "10320"),
        "daily_wage": "",
        "work_location": emp.get("work_location") or "본사",
        "job_duty": emp.get("job_duty") or "",
    }


def build_context(emp: dict, params: dict, settings: dict | None = None) -> dict:
    s = settings or get_settings()
    salary = int(params.get("annual_salary") or 0)
    sd, ed = to_date(params.get("start_date")), to_date(params.get("end_date"))
    period = ""
    if sd and ed:
        y, m = service_period(sd, ed + timedelta(days=1))
        period = f"{y}년 {m}개월" if y else f"{m}개월"
    sy, sm = service_period(emp.get("hire_date"), emp.get("resign_date"))
    return {
        "회사명": s.get("company_name", ""),
        "대표자": s.get("ceo_name", ""),
        "회사주소": s.get("company_address", ""),
        "사업자번호": s.get("business_no", ""),
        "회사전화": s.get("company_phone", ""),
        "성명": emp.get("name", ""),
        "사번": emp.get("emp_no", ""),
        "생년월일": _kdate(emp.get("birth_date")),
        "주소": emp.get("address") or "",
        "연락처": emp.get("phone") or "",
        "이메일": emp.get("email") or "",
        "부서": emp.get("department") or "",
        "직급": emp.get("rank") or "",
        "직책": emp.get("title") or "",
        "담당업무": params.get("job_duty") or emp.get("job_duty") or "",
        "고용형태": emp.get("employment_type") or "",
        "근무장소": params.get("work_location") or emp.get("work_location") or "",
        "입사일": _kdate(emp.get("hire_date")),
        "퇴사일": _kdate(emp.get("resign_date")) or "현재",
        "재직기간": f"{sy}년 {sm}개월",
        "계약시작일": _kdate(sd),
        "계약종료일": _kdate(ed) or "기간의 정함 없음",
        "계약기간": period,
        "수습기간": params.get("probation") or "없음",
        "근무요일": params.get("work_days") or "",
        "시업시각": params.get("start_time") or "",
        "종업시각": params.get("end_time") or "",
        "휴게시간": params.get("break_time") or "",
        "주소정근로시간": str(params.get("weekly_hours") or ""),
        "연봉": won(salary) if salary else "",
        "연봉_한글": korean_amount(salary) if salary else "",
        "월급여": won(salary / 12) if salary else "",
        "시급": won(params.get("hourly_wage")) if params.get("hourly_wage") else "",
        "일급": won(params.get("daily_wage")) if params.get("daily_wage") else "",
        "임금지급일": s.get("pay_day", "25"),
        "작성일": _kdate(params.get("doc_date") or today()),
        "발급번호": params.get("doc_no") or "",
        "용도": params.get("purpose") or "",
    }


def render_template(content: str, ctx: dict) -> str:
    """{{치환자}} 치환. 값이 없는 치환자는 눈에 띄도록 표시."""
    def rep(m):
        key = m.group(1)
        if key in ctx and ctx[key] != "":
            return str(ctx[key])
        return f'<span class="ph-missing">[{key}]</span>'
    return _PH.sub(rep, content)


def suggest_template_category(emp: dict) -> str:
    return EMPLOYMENT_TO_CONTRACT.get(emp.get("employment_type"), "정규직")


# ════════════════════════════════════════════════════════════════════════
# 연봉 분석
# ════════════════════════════════════════════════════════════════════════
def _in_service(emp: dict, year: int) -> bool:
    hd, rd = to_date(emp.get("hire_date")), to_date(emp.get("resign_date"))
    if hd and hd.year > year:
        return False
    if rd and rd.year < year:
        return False
    return True


def salary_overview(year: int) -> dict:
    emps = query("SELECT * FROM employees ORDER BY department, emp_no")
    sals = repo.all_salaries()
    by_emp = defaultdict(dict)
    for s in sals:
        by_emp[s["employee_id"]][s["year"]] = s
    settings = get_settings()
    min_wage = float(settings.get("min_wage_hourly") or 0)
    std_hours = float(settings.get("monthly_std_hours") or 209)

    rows = []
    for e in emps:
        if not _in_service(e, year):
            continue
        hist = by_emp.get(e["id"], {})
        cur = hist.get(year)
        prev_years = [y for y in hist if y < year]
        prev = hist[max(prev_years)] if prev_years else None
        r = {
            "employee": e,
            "cur": cur,
            "prev": prev,
            "raise_amount": None,
            "raise_rate": None,
            "below_min_wage": False,
            "hourly": None,
        }
        if cur and prev:
            r["raise_amount"] = cur["annual_salary"] - prev["annual_salary"]
            r["raise_rate"] = r["raise_amount"] / prev["annual_salary"] * 100 if prev["annual_salary"] else None
        base = cur or prev
        if base and e.get("employment_type") not in ("단시간(파트타임)", "일용직"):
            r["hourly"] = base["annual_salary"] / 12 / std_hours
            r["below_min_wage"] = min_wage > 0 and r["hourly"] < min_wage
        rows.append(r)

    with_cur = [r for r in rows if r["cur"]]
    raised = [r for r in rows if r["raise_rate"] is not None]
    total = sum(r["cur"]["annual_salary"] for r in with_cur)
    prev_total = sum(r["prev"]["annual_salary"] for r in raised)
    cur_total_of_raised = sum(r["cur"]["annual_salary"] for r in raised)

    # 부서·직급별 평균
    dept = defaultdict(list)
    rank = defaultdict(list)
    for r in with_cur:
        dept[r["employee"]["department"] or "미지정"].append(r["cur"]["annual_salary"])
        rank[r["employee"]["rank"] or "미지정"].append(r["cur"]["annual_salary"])
    from app.db import get_codes
    rank_order = {n: i for i, n in enumerate(get_codes("RANK"))}

    # 인상률 분포 구간
    bins = [("동결·삭감", None, 0.0001), ("0~3%", 0.0001, 3), ("3~5%", 3, 5),
            ("5~7%", 5, 7), ("7~10%", 7, 10), ("10% 이상", 10, None)]
    dist = []
    for label, lo, hi in bins:
        cnt = sum(1 for r in raised if (lo is None or r["raise_rate"] >= lo) and (hi is None or r["raise_rate"] < hi))
        dist.append({"label": label, "count": cnt})

    rates = sorted(r["raise_rate"] for r in raised)
    median = rates[len(rates) // 2] if rates else None

    return {
        "year": year,
        "rows": rows,
        "missing": [r for r in rows if not r["cur"] and not r["employee"].get("resign_date")],
        "kpi": {
            "headcount": len(with_cur),
            "total": total,
            "avg": total / len(with_cur) if with_cur else 0,
            "avg_rate": sum(rates) / len(rates) if rates else None,
            "median_rate": median,
            "total_rate": (cur_total_of_raised - prev_total) / prev_total * 100 if prev_total else None,
            "raise_total": cur_total_of_raised - prev_total,
            "max_rate": max(rates) if rates else None,
            "min_rate": min(rates) if rates else None,
            "below_min_wage": sum(1 for r in rows if r["below_min_wage"]),
        },
        "dept_avg": sorted(({"name": k, "avg": sum(v) / len(v), "count": len(v)} for k, v in dept.items()),
                           key=lambda x: -x["avg"]),
        "rank_avg": sorted(({"name": k, "avg": sum(v) / len(v), "count": len(v)} for k, v in rank.items()),
                           key=lambda x: rank_order.get(x["name"], 99)),
        "distribution": dist,
    }


def salary_trend() -> list[dict]:
    """연도별 인건비 총액·평균연봉·평균인상률."""
    sals = repo.all_salaries()
    by_year = defaultdict(list)
    by_emp = defaultdict(dict)
    for s in sals:
        by_year[s["year"]].append(s["annual_salary"])
        by_emp[s["employee_id"]][s["year"]] = s["annual_salary"]
    out = []
    for y in sorted(by_year):
        rates = []
        for hist in by_emp.values():
            if y in hist:
                prev = [py for py in hist if py < y]
                if prev and hist[max(prev)]:
                    rates.append((hist[y] - hist[max(prev)]) / hist[max(prev)] * 100)
        v = by_year[y]
        out.append({
            "year": y, "total": sum(v), "avg": sum(v) / len(v), "count": len(v),
            "avg_rate": sum(rates) / len(rates) if rates else None,
        })
    return out


def bulk_raise_preview(year: int, rate: float, round_unit: int = 10000, only_missing=True,
                       department: str = "") -> list[dict]:
    """일괄 인상 시뮬레이션: 직전 연봉 × (1+rate%) → 반올림 단위 적용."""
    ov = salary_overview(year)
    out = []
    for r in ov["rows"]:
        e = r["employee"]
        if e.get("resign_date"):
            continue
        if department and e.get("department") != department:
            continue
        if only_missing and r["cur"]:
            continue
        base = r["prev"]["annual_salary"] if r["prev"] else (r["cur"]["annual_salary"] if r["cur"] else None)
        if not base:
            continue
        new = base * (1 + rate / 100)
        if round_unit:
            new = round(new / round_unit) * round_unit
        out.append({
            "employee_id": e["id"], "name": e["name"], "department": e["department"], "rank": e["rank"],
            "base": base, "new": int(new), "diff": int(new - base),
            "rate": (new - base) / base * 100,
        })
    return out


# ════════════════════════════════════════════════════════════════════════
# 대시보드
# ════════════════════════════════════════════════════════════════════════
def dashboard() -> dict:
    t = today()
    settings = get_settings()
    alert_days = int(settings.get("alert_days") or 60)
    emps = repo.list_employees()
    active = [e for e in emps if e["status"] in ("재직", "휴직", "단축근무")]
    resigned = [e for e in emps if e["status"] == "퇴사"]

    # ── KPI ──
    month_key = t.strftime("%Y-%m")
    year_key = str(t.year)
    hires_m = [e for e in emps if (e["hire_date"] or "").startswith(month_key)]
    resign_m = [e for e in emps if (e["resign_date"] or "").startswith(month_key)]
    resign_y = [e for e in emps if (e["resign_date"] or "").startswith(year_key)]
    start_of_year = sum(
        1 for e in emps
        if to_date(e["hire_date"]) and to_date(e["hire_date"]).year < t.year
        and (not to_date(e["resign_date"]) or to_date(e["resign_date"]).year >= t.year)
    )
    avg_headcount = (start_of_year + len(active)) / 2 or 1
    turnover = len(resign_y) / avg_headcount * 100

    salaries = [e["annual_salary"] for e in active if e["annual_salary"]]
    tenure = [service_years_float(e["hire_date"]) for e in active]

    kpi = {
        "active": len(active),
        "on_leave": sum(1 for e in active if e["status"] == "휴직"),
        "reduced": sum(1 for e in active if e["status"] == "단축근무"),
        "hires_month": len(hires_m),
        "resign_month": len(resign_m),
        "hires_year": sum(1 for e in emps if (e["hire_date"] or "").startswith(year_key)),
        "resign_year": len(resign_y),
        "turnover": turnover,
        "avg_tenure": sum(tenure) / len(tenure) if tenure else 0,
        "avg_salary": sum(salaries) / len(salaries) if salaries else 0,
        "payroll": sum(salaries),
        "contract_missing": sum(1 for e in active if not e["has_contract"]),
        "female_ratio": (sum(1 for e in active if e.get("gender") == "여") / len(active) * 100) if active else 0,
    }

    # ── 차트 데이터 ──
    dept_cnt = Counter(e["department"] or "미지정" for e in active)
    type_cnt = Counter(e["employment_type"] or "미지정" for e in active)
    months = [add_months(t.replace(day=1), -i) for i in range(11, -1, -1)]
    flow = []
    for m in months:
        k = m.strftime("%Y-%m")
        flow.append({
            "label": m.strftime("%y.%m"),
            "hire": sum(1 for e in emps if (e["hire_date"] or "").startswith(k)),
            "resign": sum(1 for e in emps if (e["resign_date"] or "").startswith(k)),
        })
    tenure_bins = [("1년 미만", 0, 1), ("1~3년", 1, 3), ("3~5년", 3, 5), ("5~10년", 5, 10), ("10년 이상", 10, 99)]
    tenure_dist = [{"label": l, "count": sum(1 for v in tenure if lo <= v < hi)} for l, lo, hi in tenure_bins]
    ages = []
    for e in active:
        b = to_date(e.get("birth_date"))
        if b:
            ages.append(t.year - b.year - ((t.month, t.day) < (b.month, b.day)))
    age_bins = [("20대", 20, 30), ("30대", 30, 40), ("40대", 40, 50), ("50대", 50, 60), ("60대+", 60, 120)]
    age_dist = [{"label": l, "count": sum(1 for a in ages if lo <= a < hi)} for l, lo, hi in age_bins]

    return {
        "kpi": kpi,
        "alerts": build_alerts(emps, alert_days),
        "charts": {
            "dept": [{"label": k, "count": v} for k, v in dept_cnt.most_common()],
            "type": [{"label": k, "count": v} for k, v in type_cnt.most_common()],
            "flow": flow,
            "tenure": tenure_dist,
            "age": age_dist,
            "salary_trend": salary_trend(),
        },
        "recent_appointments": repo.list_appointments(limit=8),
        "recent_rewards": repo.list_rewards()[:5],
        "active_specials": [s for s in repo.list_specials() if s["status"] in ("진행중", "예정")][:8],
    }


def build_alerts(emps: list[dict], alert_days: int = 60) -> list[dict]:
    """관리 리스크 알림. level: danger(법적 리스크) / warning(기한 임박) / info(참고)."""
    t = today()
    alerts = []
    active = [e for e in emps if e["status"] in ("재직", "휴직", "단축근무")]

    def add(level, cat, emp, msg, d=None, link=None):
        alerts.append({
            "level": level, "category": cat, "name": emp.get("name") if emp else "",
            "emp_id": emp.get("id") if emp else None, "message": msg,
            "dday": days_until(d) if d else None, "link": link,
        })

    # 1) 근로계약서 미체결 (근로기준법 제17조: 서면 명시·교부 의무, 위반 시 500만원 이하 벌금)
    for e in active:
        if not e["has_contract"]:
            add("danger", "근로계약", e, "유효한 근로계약서(체결완료)가 없습니다 — 근로기준법 제17조 서면교부 의무",
                link=f"/hr/contracts/new?employee_id={e['id']}")

    # 2) 기간제 계약 만료 임박 / 2년 초과 사용 (기간제법 제4조: 무기계약 간주)
    docs = repo.list_documents("contract", status="체결완료")
    latest_end = {}
    for d in docs:
        if d["category"] == "연봉계약" or not d["end_date"]:
            continue
        if d["employee_id"] not in latest_end or d["end_date"] > latest_end[d["employee_id"]]["end_date"]:
            latest_end[d["employee_id"]] = d
    for emp_id, d in latest_end.items():
        e = next((x for x in active if x["id"] == emp_id), None)
        if not e:
            continue
        left = days_until(d["end_date"])
        if left is not None and 0 <= left <= alert_days:
            add("warning", "계약만료", e, f"근로계약 만료 예정 ({d['end_date']}) — 갱신·종료 통보 필요",
                d["end_date"], f"/hr/contracts/{d['id']}")
        elif left is not None and left < 0:
            add("danger", "계약만료", e, f"근로계약 기간 경과 ({d['end_date']}) — 재계약 미체결 상태",
                d["end_date"], f"/hr/contracts/new?employee_id={e['id']}")
    for e in active:
        if e["employment_type"] in ("계약직(기간제)", "촉탁직", "인턴"):
            two = add_years(to_date(e["hire_date"]), 2)
            left = (two - t).days
            if left <= 90:
                add("danger" if left < 0 else "warning", "기간제 2년", e,
                    f"기간제 사용기간 2년 {'초과' if left < 0 else '도래'} ({two}) — 무기계약 전환 검토 (기간제법 제4조)",
                    two.isoformat(), f"/hr/employees/{e['id']}")

    # 3) 수습 종료
    for e in active:
        pe = to_date(e.get("probation_end"))
        if pe and 0 <= (pe - t).days <= 30:
            add("warning", "수습종료", e, f"수습기간 종료 예정 ({pe}) — 정규 전환 평가 필요", pe.isoformat(),
                f"/hr/employees/{e['id']}")

    # 4) 휴직·단축근무 종료 → 복직 준비
    for s in repo.list_specials():
        if s["status"] != "진행중" or not s["end_date"] or s["actual_end"]:
            continue
        left = days_until(s["end_date"])
        if left is not None and 0 <= left <= alert_days:
            emp = {"id": s["employee_id"], "name": s["name"]}
            if s["category"] in LEAVE_CATEGORIES:
                add("warning", "복직예정", emp, f"{s['category']} 종료 예정 ({s['end_date']}) — 복직 면담·직무 배치 준비",
                    s["end_date"], "/hr/specials")
            elif s["category"] in REDUCE_CATEGORIES:
                add("info", "단축종료", emp, f"{s['category']} 종료 예정 ({s['end_date']}) — 근무시간 원복 및 급여 조정",
                    s["end_date"], "/hr/specials")

    # 5) 당해 연봉 미책정 / 최저임금 미달 의심
    ov = salary_overview(t.year)
    missing = [r for r in ov["missing"] if r["employee"]["id"] in {e["id"] for e in active}]
    if missing:
        alerts.append({
            "level": "info", "category": "연봉책정", "name": f"{len(missing)}명", "emp_id": None,
            "message": f"{t.year}년 연봉이 등록되지 않은 재직자 {len(missing)}명 — 연봉계약 진행 필요",
            "dday": None, "link": f"/hr/salaries?year={t.year}",
        })
    for r in ov["rows"]:
        if r["below_min_wage"] and not r["employee"].get("resign_date"):
            add("danger", "최저임금", r["employee"],
                f"환산 시급 {won(r['hourly'])}원 — 최저임금 미달 의심 (기본연봉÷12÷월소정시간 기준, 수당 구성 확인 필요)",
                link=f"/hr/employees/{r['employee']['id']}")

    # 6) 징계 말소 예정
    for rw in repo.list_rewards("징계"):
        left = days_until(rw.get("expunge_date"))
        if left is not None and 0 <= left <= 30:
            add("info", "징계말소", {"id": rw["employee_id"], "name": rw["name"]},
                f"{rw['category']} 징계 효력 말소 예정 ({rw['expunge_date']}) — 인사평가·승진 제한 해제",
                rw["expunge_date"], "/hr/rewards")

    # 7) 근속 기념 (이번 달) — 포상 검토
    for e in active:
        hd = to_date(e["hire_date"])
        if hd and hd.month == t.month:
            yrs = t.year - hd.year
            if yrs in (5, 10, 15, 20, 25, 30):
                add("info", "장기근속", e, f"입사 {yrs}주년 ({hd.replace(year=t.year)}) — 장기근속 포상 검토",
                    hd.replace(year=t.year).isoformat(), "/hr/rewards")

    order = {"danger": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: (order[a["level"]], a["dday"] if a["dday"] is not None else 9999))
    return alerts
