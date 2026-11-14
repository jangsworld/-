"""총괄 대시보드: 인사·총무 대시보드의 핵심 카드·그래프를 한 화면에, 아래에 '지금 챙길 일'을 모아 봅니다.

권한에 맞는 구역만 계산합니다 (조회자에게 연봉·인사 알림·법정의무를 계산하거나 보내지 않음).
"""
import logging
from datetime import timedelta

from app.common import today
from app.db import get_settings

log = logging.getLogger("bms.home")
LEVELS = {"danger": 0, "warning": 1, "info": 2}


def _safe(fn, default):
    """한 구역 계산이 실패해도 대시보드 전체가 멈추지 않도록."""
    try:
        return fn()
    except Exception:                       # noqa: BLE001
        log.exception("총괄 대시보드 구역 계산 실패")
        return default


def _sorted(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda a: (LEVELS.get(a["level"], 3), a["dday"] if a.get("dday") is not None else 9999))


def _hr(kpi: dict) -> list[dict]:
    from app.modules.hr import repo, services
    alert_days = int(get_settings().get("alert_days") or 60)
    out = services.build_alerts(repo.list_employees(), alert_days)
    from app.modules.hr import ecount
    n = len(ecount.pending())
    if n:
        out.append({"level": "info", "category": "이카운트", "name": f"{n}건", "emp_id": None, "dday": None,
                    "message": f"이카운트에 아직 반영하지 않은 입사·퇴사 {n}건", "link": "/hr/ecount"})
    return _sorted(out)


def _duties() -> list[dict]:
    """법정의무·교육 기한: 지난 것 + 14일 안 (연말이면 다음 해 1월 일정도)."""
    from app.modules.mgmt import compliance
    t = today()
    years = [t.year] + ([t.year + 1] if t.month == 12 else [])
    out = []
    for y in years:
        for e in compliance.calendar_year(y, ("의무", "교육")):
            if e["status"] not in ("overdue", "soon"):
                continue
            if e["date"] > t + timedelta(days=30):
                continue
            left = (e["date"] - t).days
            out.append({"level": "danger" if left < 0 else "warning", "category": e["source"] + "·" + e["category"],
                        "name": e["title"], "emp_id": None, "dday": left,
                        "message": e.get("detail") or (f"기한 {e['date']}" + (f" · {e['basis']}" if e.get("basis") else "")),
                        "link": e.get("link") or f"/mgmt/compliance?year={y}"})
    return _sorted(out)


def _ga() -> dict:
    from app.modules.ga import services
    return services.dashboard()


def build(role: str) -> dict:
    from app.modules.hr import services as hr_services
    staff = role in ("admin", "hr")
    ga_staff = role in ("admin", "hr", "ga")
    hd = _safe(hr_services.dashboard, {"kpi": {}})
    k = hd.get("kpi", {})
    kpi = {"active": k.get("active", 0), "on_leave": k.get("on_leave", 0), "reduced": k.get("reduced", 0),
           "hires_month": k.get("hires_month", 0), "resign_month": k.get("resign_month", 0)}
    if staff:
        def labor_kpi():
            from app.modules.mgmt import labor
            ov = labor.overview(today().year)
            return {"labor_total": ov["total"]["total"], "labor_ratio": ov["labor_ratio"],
                    "contract_missing": k.get("contract_missing", 0)}
        kpi.update(_safe(labor_kpi, {}))
    # 인사 현황 카드·그래프 (연봉 정보는 관리자·인사담당에게만)
    hc = hd.get("charts", {})
    hr_view = {"kpi": {x: k.get(x, 0) for x in ("hires_year", "resign_year", "turnover", "avg_tenure", "female_ratio")},
               "charts": {"dept": hc.get("dept", []), "type": hc.get("type", []), "flow": hc.get("flow", [])}}
    if staff:
        hr_view["kpi"].update({x: k.get(x, 0) for x in ("avg_salary", "payroll", "contract_missing")})
        hr_view["charts"]["salary_trend"] = hc.get("salary_trend", [])
    # 총무 현황 카드·그래프 (경조금은 총무 담당자 이상만)
    gd = _safe(_ga, {"kpi": {}, "charts": {}, "alerts": [], "vstat": {}})
    gk = dict(gd.get("kpi", {}))
    if not ga_staff:
        gk.pop("events_year", None)
        gk.pop("events_count", None)
    gc = gd.get("charts", {})
    ga_view = {"kpi": gk, "year": (gd.get("vstat") or {}).get("year", today().year),
               "charts": {"asset_cat": gc.get("asset_cat", []), "fixed": gc.get("fixed", []), "vehicle": gc.get("vehicle", [])}}
    sections = []
    if staff:
        sections.append({"key": "hr", "title": "인사", "icon": "bi-people", "alerts": _safe(lambda: _hr(kpi), []),
                         "link": "/hr/dashboard"})
    if ga_staff:
        sections.append({"key": "duty", "title": "교육 · 법정의무", "icon": "bi-calendar2-check", "alerts": _safe(_duties, []),
                         "link": "/mgmt/compliance"})
    ga_items = _sorted(list(gd.get("alerts", [])))
    if not ga_staff:                        # 경조사(가족 사망·질병 등)는 총무 담당자 이상만
        ga_items = [a for a in ga_items if a["category"] != "경조금"]
    sections.append({"key": "ga", "title": "총무", "icon": "bi-building", "alerts": ga_items, "link": "/ga/dashboard"})
    for s in sections:
        s["counts"] = {lv: sum(1 for a in s["alerts"] if a["level"] == lv) for lv in LEVELS}
    kpi["urgent"] = sum(s["counts"]["danger"] for s in sections)
    kpi["soon"] = sum(s["counts"]["warning"] for s in sections)
    return {"kpi": kpi, "sections": sections, "today": today(), "hr": hr_view, "ga": ga_view}
