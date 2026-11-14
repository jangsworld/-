"""총괄 대시보드: 인사·교육·법정의무·총무에서 '지금 챙겨야 할 것'만 모아 봅니다.

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


def _ga() -> list[dict]:
    from app.modules.ga import repo, services
    alert_days = int(get_settings().get("alert_days") or 60)
    assets = services.enrich_assets(repo.list_assets())
    contracts = [services.contract_view(c) for c in repo.list_contracts(status="유효")]
    return _sorted(services.build_alerts(assets, repo.list_vehicles(), contracts, alert_days))


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
    sections = []
    if staff:
        sections.append({"key": "hr", "title": "인사", "icon": "bi-people", "alerts": _safe(lambda: _hr(kpi), []),
                         "link": "/hr/dashboard"})
    if ga_staff:
        sections.append({"key": "duty", "title": "교육 · 법정의무", "icon": "bi-calendar2-check", "alerts": _safe(_duties, []),
                         "link": "/mgmt/compliance"})
    ga_items = _safe(_ga, [])
    if not ga_staff:                        # 경조사(가족 사망·질병 등)는 총무 담당자 이상만
        ga_items = [a for a in ga_items if a["category"] != "경조금"]
    sections.append({"key": "ga", "title": "총무", "icon": "bi-building", "alerts": ga_items, "link": "/ga/dashboard"})
    for s in sections:
        s["counts"] = {lv: sum(1 for a in s["alerts"] if a["level"] == lv) for lv in LEVELS}
    kpi["urgent"] = sum(s["counts"]["danger"] for s in sections)
    kpi["soon"] = sum(s["counts"]["warning"] for s in sections)
    return {"kpi": kpi, "sections": sections, "today": today()}
