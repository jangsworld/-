"""총무관리 비즈니스 로직: 감가상각, 차량 업무사용비율, 계약 만기·통보기한, 대시보드·알림."""
from collections import defaultdict
from datetime import timedelta

from app.common import today, to_date, add_months, days_until, vat_split
from app.db import get_settings
from app.modules.ga import repo
from app.modules.ga.constants import PAY_CYCLES


# ── 자산 감가상각 (정액법, 월할) ───────────────────────────────────────
def depreciation(asset: dict, ref=None) -> dict:
    ref = ref or today()
    cost = vat_split(asset.get("cost"), asset.get("cost_vat"))["supply"]   # 감가상각은 공급가액 기준
    life = max(1, asset.get("useful_years") or 5)
    acq = to_date(asset.get("acquired_date"))
    end = to_date(asset.get("disposed_date")) or ref
    if not acq or not cost:
        return {"months": 0, "accumulated": 0, "book": cost, "ratio": 0}
    months = max(0, (end.year - acq.year) * 12 + end.month - acq.month + 1)
    total = life * 12
    acc = min(cost, round(cost * months / total))
    book = cost - acc
    if months >= total:
        book, acc = min(1000, cost), cost - min(1000, cost)   # 상각 완료 후 비망가액 1,000원
    return {"months": min(months, total), "accumulated": acc, "book": book,
            "ratio": min(100, months / total * 100), "done": months >= total}


def enrich_assets(rows: list[dict]) -> list[dict]:
    for a in rows:
        a.update(depreciation(a))
        a.update(vat_split(a.get("cost"), a.get("cost_vat")))
        a["warranty_dday"] = days_until(a.get("warranty_end"))
    return rows


# ── 차량 ────────────────────────────────────────────────────────────────
def vehicle_stats(vehicle_id=None, year=None) -> dict:
    year = year or today().year
    logs = repo.vehicle_logs(vehicle_id, year)
    maint = repo.vehicle_maint(vehicle_id, year)
    total = sum(l["distance"] for l in logs)
    biz = sum(l["distance"] for l in logs if l["purpose"] in ("일반업무", "출퇴근"))
    fuel = sum(l["fuel_cost"] or 0 for l in logs)
    toll = sum(l["toll_cost"] or 0 for l in logs)
    mcost = sum(vat_split(m["cost"], m.get("cost_vat"))["total"] for m in maint)
    monthly = defaultdict(lambda: {"km": 0, "fuel": 0})
    for l in logs:
        k = l["drive_date"][:7]
        monthly[k]["km"] += l["distance"]
        monthly[k]["fuel"] += (l["fuel_cost"] or 0) + (l["toll_cost"] or 0)
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    return {
        "year": year, "trips": len(logs), "total_km": total, "biz_km": biz,
        "suspect": sum(1 for l in logs if l["suspect"]),
        "biz_ratio": biz / total * 100 if total else None,
        "fuel": fuel, "toll": toll, "maint": mcost, "cost_total": fuel + toll + mcost,
        "cost_per_km": (fuel + toll) / total if total else None,
        "monthly": [{"label": m[5:] + "월", **monthly[m]} for m in months],
    }


# ── 계약 ────────────────────────────────────────────────────────────────
def contract_view(c: dict) -> dict:
    t = today()
    end = to_date(c.get("end_date"))
    c["dday"] = (end - t).days if end else None
    c["notice_date"] = (end - timedelta(days=c.get("notice_days") or 0)).isoformat() if end else None
    c["notice_dday"] = days_until(c["notice_date"])
    months = PAY_CYCLES.get(c.get("pay_cycle") or "월", 1)
    c["monthly"] = vat_split(c.get("amount"), c.get("amount_vat"))["total"] / months if months else 0
    nd, dd = c["notice_dday"], c["dday"]
    if c["status"] != "유효":
        c["phase"] = c["status"]
    elif end and end < t:
        c["phase"] = "자동갱신" if c.get("auto_renew") else "만료"
    elif c.get("auto_renew") and nd is not None and nd < 0:
        c["phase"] = "갱신확정"
    elif nd is not None and 0 <= nd <= 14:
        c["phase"] = "통보임박"
    elif nd is not None and nd < 0:
        c["phase"] = "통보경과"
    elif dd is not None and dd <= int(get_settings().get("alert_days") or 60):
        c["phase"] = "만료임박"
    else:
        c["phase"] = "유효"
    return c


def roll_auto_renew():
    """자동갱신 계약이 만료일을 지나면 같은 기간만큼 연장(변경은 감사로그에 기록)."""
    for c in repo.list_contracts(status="유효"):
        s, e = to_date(c["start_date"]), to_date(c["end_date"])
        if not (c["auto_renew"] and s and e and e < today()):
            continue
        nxt = e + timedelta(days=1)
        months = max(1, (nxt.year - s.year) * 12 + nxt.month - s.month)
        while e < today():
            s = e + timedelta(days=1)
            e = add_months(s, months) - timedelta(days=1)
        repo.save("ga_contracts", c["id"], {"start_date": s.isoformat(), "end_date": e.isoformat()})


# ── 대시보드 ────────────────────────────────────────────────────────────
def dashboard() -> dict:
    t = today()
    alert_days = int(get_settings().get("alert_days") or 60)
    assets = enrich_assets(repo.list_assets())
    live_assets = [a for a in assets if a["status"] != "폐기"]
    vehicles = repo.list_vehicles()
    vstat = vehicle_stats()
    contracts = [contract_view(c) for c in repo.list_contracts(status="유효")]
    events = repo.list_events(year=t.year)
    month = t.strftime("%Y-%m")
    docs_m = [d for d in repo.list_docs(year=t.year) if d["doc_date"].startswith(month)]

    by_cat = defaultdict(lambda: {"count": 0, "book": 0})
    for a in live_assets:
        by_cat[a["category"]]["count"] += 1
        by_cat[a["category"]]["book"] += a["book"]
    cmonthly = defaultdict(float)
    for c in contracts:
        cmonthly[c["category"]] += c["monthly"]
    ev_month = defaultdict(int)
    for e in events:
        if e["status"] != "취소":
            ev_month[int(e["event_date"][5:7])] += e["amount"] or 0

    kpi = {
        "assets": len(live_assets),
        "asset_cost": sum(a["supply"] for a in live_assets),
        "asset_book": sum(a["book"] for a in live_assets),
        "vehicles": sum(1 for v in vehicles if v["status"] != "매각"),
        "km_year": vstat["total_km"], "biz_ratio": vstat["biz_ratio"], "vcost_year": vstat["cost_total"],
        "contracts": len(contracts),
        "fixed_monthly": sum(c["monthly"] for c in contracts),
        "deposits": sum(c["deposit"] or 0 for c in contracts),
        "docs_month": len(docs_m),
        "events_year": sum(e["amount"] or 0 for e in events if e["status"] != "취소"),
        "events_count": sum(1 for e in events if e["status"] != "취소"),
    }
    return {
        "kpi": kpi, "alerts": build_alerts(assets, vehicles, contracts, alert_days), "vstat": vstat,
        "charts": {
            "asset_cat": [{"label": k, **v} for k, v in sorted(by_cat.items(), key=lambda x: -x[1]["book"])],
            "fixed": [{"label": k, "value": v} for k, v in sorted(cmonthly.items(), key=lambda x: -x[1])],
            "events": [{"label": f"{m}월", "value": ev_month[m]} for m in range(1, 13)],
            "vehicle": vstat["monthly"],
        },
        "pending_seals": repo.list_seals(status="신청")[:6],
        "recent_docs": repo.list_docs()[:6],
        "recent_events": events[:5],
    }


def build_alerts(assets, vehicles, contracts, alert_days=60) -> list[dict]:
    t = today()
    out = []

    def add(level, cat, title, msg, d=None, link=None):
        out.append({"level": level, "category": cat, "name": title, "message": msg,
                    "dday": days_until(d) if d else None, "link": link, "emp_id": None})

    # 1) 퇴사자 미반납 자산 (인사 연동)
    for a in assets:
        rd = to_date(a.get("holder_resign"))
        if a["holder_id"] and rd and rd <= t and a["status"] != "폐기":
            add("danger", "자산회수", a["asset_no"], f"퇴사자 {a['holder_name']}({rd}) 보유 자산 미반납 — {a['name']}",
                link=f"/ga/assets/{a['id']}")
    # 2) 보증·라이선스 만료 (만료 30일 전 ~ 만료 후 90일까지만 알림 – 오래된 보증만료는 자산 화면에서 확인)
    for a in assets:
        dd = a.get("warranty_dday")
        if a["status"] == "폐기" or dd is None or not (-90 <= dd <= 30):
            continue
        is_sw = a["category"] == "소프트웨어·라이선스"
        add("danger" if (dd < 0 and is_sw) else "warning", "라이선스" if is_sw else "보증만료", a["asset_no"],
            f"{a['name']} {'라이선스 갱신' if is_sw else 'AS 보증'} {'만료' if dd < 0 else '만료 예정'} ({a['warranty_end']})",
            a["warranty_end"], f"/ga/assets/{a['id']}")
    # 3) 차량 보험·검사·리스 만기
    for v in vehicles:
        if v["status"] == "매각":
            continue
        for field, label, lv_days in (("insurance_end", "자동차보험 만기", 30), ("inspection_due", "정기검사 기한", 30),
                                      ("lease_end", "리스·렌트 만기", 90)):
            dd = days_until(v.get(field))
            if dd is not None and dd <= lv_days:
                extra = " — 무보험 운행 금지" if field == "insurance_end" and dd < 0 else \
                        (" — 기한 경과 시 과태료" if field == "inspection_due" else "")
                add("danger" if dd < 0 else "warning", "차량", v["plate_no"], f"{label} ({v[field]}){extra}",
                    v[field], f"/ga/vehicles/{v['id']}")
    # 4) 계약: 자동갱신은 '해지 통보기한', 수동갱신은 '만료일' 기준
    for c in contracts:
        if c["auto_renew"]:
            nd = c["notice_dday"]
            if nd is not None and 0 <= nd <= alert_days:
                add("danger" if nd <= 14 else "warning", "계약통보", c["counterparty"],
                    f"{c['title']} — 해지하려면 {c['notice_date']}까지 통보 (미통보 시 자동 갱신)", c["notice_date"], "/ga/contracts")
            elif nd is not None and nd < 0 and c["dday"] is not None and c["dday"] >= 0:
                add("info", "자동갱신", c["counterparty"], f"{c['title']} — 통보기한 경과, {c['end_date']} 이후 자동 갱신 확정",
                    c["end_date"], "/ga/contracts")
        else:
            dd = c["dday"]
            if dd is not None and dd < 0:
                add("danger", "계약만료", c["counterparty"], f"{c['title']} 계약기간 만료({c['end_date']}) — 재계약·종료 처리",
                    c["end_date"], "/ga/contracts")
            elif dd is not None and dd <= max(alert_days, c.get("notice_days") or 0):
                add("warning", "계약만료", c["counterparty"], f"{c['title']} 만료 예정 — 재계약 여부 결정 (통보기한 {c['notice_date']})",
                    c["end_date"], "/ga/contracts")
    # 5) 관인 사용 승인 대기
    n = len(repo.list_seals(status="신청"))
    if n:
        out.append({"level": "info", "category": "관인", "name": f"{n}건", "message": f"관인 사용 승인 대기 {n}건",
                    "dday": None, "link": "/ga/docs?tab=seal", "emp_id": None})
    # 6) 회신 기한 문서
    for d in repo.list_docs(status="회신필요"):
        dd = days_until(d.get("due_date"))
        if dd is not None and dd <= 7:
            add("danger" if dd < 0 else "warning", "회신기한", d["doc_no"], f"{d['counterpart']} — {d['title']}",
                d["due_date"], "/ga/docs")
    # 7) 경조금 미지급
    for e in repo.list_events(status="신청"):
        if (t - to_date(e["event_date"])).days >= 3:
            out.append({"level": "info", "category": "경조금", "name": e["name"], "emp_id": e["employee_id"],
                        "message": f"{e['relation']} {e['category']} 경조금 {e['amount']:,}원 미지급 ({e['event_date']})",
                        "dday": None, "link": "/ga/events"})
    order = {"danger": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (order[a["level"]], a["dday"] if a["dday"] is not None else 9999))
    return out
