"""총무관리 페이지·API 라우트 (prefix: /ga)."""
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.common import templates, today, to_date, vat_split
from app.config import settings as cfg
from app.db import audit, get_codes, get_settings, execute
from app.modules.ga import cartax, photos, repo, services
from app.modules.ga.constants import (
    ASSET_CATEGORIES, ASSET_STATUSES, VEHICLE_STATUSES, DRIVE_PURPOSES, PURPOSE_PENDING, DOC_STATUSES, SEAL_STATUSES,
    PAY_CYCLES, CONTRACT_STATUSES, EVENT_STATUSES, VAT_TYPES,
)
from app.modules.hr.routes import _csv_response, _redirect

router = APIRouter(prefix="/ga")

KINDS = {  # URL 구분 → 테이블 (화이트리스트)
    "assets": "ga_assets", "vehicles": "ga_vehicles", "vlogs": "ga_vehicle_logs", "maint": "ga_vehicle_maint",
    "docs": "ga_docs", "seals": "ga_seal_logs", "contracts": "ga_contracts", "events": "ga_events",
    "rules": "ga_event_rules",
}


def _render(request, name, ctx, active):
    ctx.setdefault("active_menu", active)
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    emps = repo.employees_brief()
    ctx.setdefault("emps", emps)
    ctx["emp_opts"] = [{"v": e["id"], "t": f"{e['name']} ({e['department'] or ''})"} for e in emps]
    ctx["can_write"] = request.state.user["role"] in ("admin", "hr", "ga")
    ctx.setdefault("vat_types", VAT_TYPES)
    return templates.TemplateResponse(request, name, ctx)


def _table(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(404)
    return KINDS[kind]


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def ga_root():
    return RedirectResponse("/ga/dashboard")


# ── 0. 대시보드 ─────────────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    services.roll_auto_renew()
    return _render(request, "ga/dashboard.html", {"d": services.dashboard()}, "ga.dashboard")


# ── 1. 자산·비품 ────────────────────────────────────────────────────────
@router.get("/assets", response_class=HTMLResponse)
def assets(request: Request, category: str = "", status: str = "", q: str = ""):
    rows = services.enrich_assets(repo.list_assets(category, status, q=q))
    live = [a for a in rows if a["status"] != "폐기"]
    return _render(request, "ga/assets.html", {
        "rows": rows, "f": {"category": category, "status": status, "q": q},
        "categories": ASSET_CATEGORIES, "statuses": ASSET_STATUSES, "locations": get_codes("ASSET_LOC"),
        "sum": {"count": len(live), "cost": sum(a["supply"] for a in live), "book": sum(a["book"] for a in live),
                "unassigned": sum(1 for a in live if not a["holder_id"])},
    }, "ga.assets")


@router.get("/assets/labels", response_class=HTMLResponse)
def asset_labels(request: Request, ids: str = ""):
    sel = {int(x) for x in ids.split(",") if x.strip().isdigit()}
    rows = [a for a in repo.list_assets() if not sel or a["id"] in sel]
    return _render(request, "ga/asset_labels.html", {"rows": rows, "public_url": _public_url()}, "ga.assets")


@router.get("/assets/export.csv")
def assets_export():
    rows = services.enrich_assets(repo.list_assets())
    out = [["자산번호", "분류", "품명", "모델", "시리얼", "취득일", "취득가", "부가세구분", "공급가액", "부가세", "합계",
            "내용연수", "감가상각누계", "장부가", "위치", "상태", "사용자", "보증만료"]]
    for a in rows:
        out.append([a["asset_no"], a["category"], a["name"], a["model"], a["serial_no"], a["acquired_date"],
                    a["cost"], a["cost_vat"], a["supply"], a["vat"], a["total"], a["useful_years"], a["accumulated"], a["book"], a["location"], a["status"],
                    a["holder_name"], a["warranty_end"]])
    audit("ga", "자산대장 내보내기", "", f"{len(rows)}건")
    return _csv_response(out, f"자산대장_{today().isoformat()}.csv")


def _public_url() -> str:
    return (get_settings().get("public_url") or cfg.public_url or "").rstrip("/")


@router.get("/a/{asset_no}")
def asset_by_no(asset_no: str):
    """라벨 QR 로 접속: 자산번호 → 상세 화면 (로그인 필요)."""
    rows = [a for a in repo.list_assets(q=asset_no) if a["asset_no"] == asset_no]
    if not rows:
        raise HTTPException(404, "자산을 찾을 수 없습니다")
    return RedirectResponse(f"/ga/assets/{rows[0]['id']}", status_code=303)


@router.get("/assets/{aid}", response_class=HTMLResponse)
def asset_detail(request: Request, aid: int):
    a = repo.get("ga_assets", aid)
    if not a:
        raise HTTPException(404, "자산을 찾을 수 없습니다")
    a = services.enrich_assets(repo.list_assets(q=a["asset_no"]))[0]
    public = _public_url()
    return _render(request, "ga/asset_detail.html", {
        "a": a, "history": repo.asset_history(aid), "events": get_codes("ASSET_EVENT"),
        "qr_text": f"{public}/ga/a/{a['asset_no']}" if public else a["asset_no"], "qr_is_url": bool(public),
        "categories": ASSET_CATEGORIES, "statuses": ASSET_STATUSES, "locations": get_codes("ASSET_LOC"),
    }, "ga.assets")


@router.get("/assets/{aid}/photo")
def asset_photo(aid: int, thumb: bool = False):
    path = photos.path(aid, thumb)
    if not os.path.exists(path):
        raise HTTPException(404, "사진이 없습니다")
    return FileResponse(path, media_type="image/webp")


def _save_photo(aid: int, upload) -> str:
    """사진 저장 후 안내 문구 반환. 사진 문제로 자산 저장이 실패하지는 않습니다."""
    try:
        size = photos.save(aid, upload.file.read())
    except photos.PhotoError as ex:
        return f" · {ex}"
    repo.save("ga_assets", aid, {"photo": "1"}, quiet=True)
    audit("ga", "자산 사진 등록", f"asset#{aid}", f"{size // 1024}KB")
    return f" · 사진 {max(1, size // 1024)}KB 로 저장"


@router.post("/assets/{aid}/photo")
async def asset_photo_upload(request: Request, aid: int):
    upload = (await request.form()).get("photo_file")
    if not getattr(upload, "filename", ""):
        return _redirect(f"/ga/assets/{aid}", "사진을 선택하세요")
    return _redirect(f"/ga/assets/{aid}", "사진 등록" + await run_in_threadpool(_save_photo, aid, upload))


@router.post("/assets/{aid}/photo/delete")
def asset_photo_delete(aid: int):
    photos.delete(aid)
    repo.save("ga_assets", aid, {"photo": None}, quiet=True)
    return _redirect(f"/ga/assets/{aid}", "사진을 삭제했습니다")


@router.post("/assets/save")
async def asset_save(request: Request):
    f = dict(await request.form())
    upload = f.pop("photo_file", None)
    rid = int(f.get("id") or 0) or None
    if not f.get("useful_years"):
        f["useful_years"] = ASSET_CATEGORIES.get(f.get("category"), ("", 5))[1]
    if not rid:
        f["asset_no"] = repo.next_asset_no(f.get("category", "기타"), f.get("acquired_date"))
        if f.get("holder_id"):
            f["status"] = "사용중"
    else:
        f.pop("asset_no", None)
        f.pop("holder_id", None)  # 사용자 변경은 지급·반납 이력으로만
    new_id = repo.save("ga_assets", rid, f)
    if not rid:
        repo.add_asset_event(new_id, f.get("acquired_date") or today().isoformat(), "취득", None, f.get("cost"), "신규 등록",
                             f.get("cost_vat"))
        if f.get("holder_id"):
            repo.add_asset_event(new_id, f.get("acquired_date") or today().isoformat(), "지급", int(f["holder_id"]), 0, "")
    msg = "저장되었습니다"
    if getattr(upload, "filename", ""):
        msg += await run_in_threadpool(_save_photo, new_id, upload)
    return _redirect(f"/ga/assets/{new_id}", msg)


@router.post("/assets/{aid}/event")
async def asset_event(request: Request, aid: int):
    f = dict(await request.form())
    holder = int(f["holder_id"]) if f.get("holder_id") else None
    if f.get("event_type") == "지급" and not holder:
        return _redirect(f"/ga/assets/{aid}", "지급 대상자를 선택하세요")
    repo.add_asset_event(aid, f.get("event_date") or today().isoformat(), f.get("event_type", "점검"), holder,
                         f.get("cost"), f.get("note", ""), f.get("cost_vat"))
    return _redirect(f"/ga/assets/{aid}", f"{f.get('event_type')} 처리되었습니다")


def _history_or_404(aid: int, hid: int) -> dict:
    h = repo.get("ga_asset_history", hid)
    if not h or h["asset_id"] != aid:
        raise HTTPException(404, "이력을 찾을 수 없습니다")
    return h


@router.post("/assets/{aid}/history/{hid}")
async def asset_history_save(request: Request, aid: int, hid: int):
    _history_or_404(aid, hid)
    f = dict(await request.form())
    f.pop("id", None)
    if repo.event_behavior(f.get("event_type", "")) == "지급" and not f.get("holder_id"):
        return _redirect(f"/ga/assets/{aid}", "지급 대상자를 선택하세요")
    f["cost"] = f.get("cost") or 0
    repo.save("ga_asset_history", hid, f)
    repo.rebuild_asset_state(aid)
    return _redirect(f"/ga/assets/{aid}", "이력을 수정했습니다. 자산 상태도 다시 계산했습니다")


@router.post("/assets/{aid}/history/{hid}/delete")
def asset_history_delete(aid: int, hid: int):
    _history_or_404(aid, hid)
    repo.delete("ga_asset_history", hid)
    repo.rebuild_asset_state(aid)
    return _redirect(f"/ga/assets/{aid}", "이력을 삭제했습니다. 자산 상태도 다시 계산했습니다")


# ── 2. 법인차량 ─────────────────────────────────────────────────────────
@router.get("/vehicles", response_class=HTMLResponse)
def vehicles(request: Request):
    rows = repo.list_vehicles()
    for v in rows:
        v["stat"] = services.vehicle_stats(v["id"])
        v["odometer"] = repo.last_odometer(v["id"])
        for k in ("insurance_end", "inspection_due", "lease_end"):
            v[k + "_dday"] = (to_date(v[k]) - today()).days if to_date(v[k]) else None
    return _render(request, "ga/vehicles.html", {
        "rows": rows, "total": services.vehicle_stats(), "ownership": get_codes("VEHICLE_OWNERSHIP"),
        "statuses": VEHICLE_STATUSES, "fuels": get_codes("FUEL"), "cartax_on": cartax.enabled(),
        "cartax_last": get_settings().get("cartax_last_sync", ""), "year_start": f"{today().year}-01-01",
    }, "ga.vehicles")


CARTAX_READONLY = "카택스 차량의 운행기록은 BMS 에서 입력·수정·삭제할 수 없습니다. 카택스에서 고쳐 주세요"


@router.post("/vehicles/cartax-sync")
async def vehicles_cartax_sync(request: Request):
    f = await request.form()
    try:
        msg = await run_in_threadpool(cartax.sync, f.get("start") or f"{today().year}-01-01", f.get("end") or today().isoformat())
    except cartax.CartaxBusy as ex:
        msg = str(ex)
    except (cartax.CartaxError, ValueError) as ex:
        msg = f"카택스 동기화 실패: {ex}"
    return _redirect("/ga/vehicles", msg)


@router.get("/vehicles/{vid}", response_class=HTMLResponse)
def vehicle_detail(request: Request, vid: int, year: int | None = None):
    v = repo.get("ga_vehicles", vid)
    if not v:
        raise HTTPException(404, "차량을 찾을 수 없습니다")
    year = year or today().year
    return _render(request, "ga/vehicle_detail.html", {
        "v": v, "year": year, "stat": services.vehicle_stats(vid, year), "logs": repo.vehicle_logs(vid, year),
        "maint": repo.vehicle_maint(vid, year), "odometer": repo.last_odometer(vid), "purposes": DRIVE_PURPOSES,
        "maint_categories": get_codes("MAINT_CATEGORY"), "ownership": get_codes("VEHICLE_OWNERSHIP"),
        "statuses": VEHICLE_STATUSES, "fuels": get_codes("FUEL"), "years": list(range(today().year, today().year - 5, -1)),
        "cartax_vehicle": repo.is_cartax_vehicle(vid),
    }, "ga.vehicles")


@router.post("/vlogs/save")
async def vlog_save(request: Request):
    f = dict(await request.form())
    vid = int(f["vehicle_id"])
    if repo.is_cartax_vehicle(vid) or repo.is_cartax_log(int(f.get("id") or 0)):
        return _redirect(f"/ga/vehicles/{vid}", CARTAX_READONLY)
    try:
        ks, ke = int(str(f["km_start"]).replace(",", "")), int(str(f["km_end"]).replace(",", ""))
    except (KeyError, ValueError):
        return _redirect(f"/ga/vehicles/{vid}", "계기판 거리를 숫자로 입력하세요")
    if ke < ks:
        return _redirect(f"/ga/vehicles/{vid}", "도착 계기판 거리가 출발보다 작습니다")
    repo.save("ga_vehicle_logs", int(f.get("id") or 0) or None, f)
    return _redirect(f"/ga/vehicles/{vid}", "운행일지가 저장되었습니다")


@router.get("/vehicles/{vid}/logbook.csv")
def vehicle_logbook(vid: int, year: int):
    """업무용승용차 운행기록부 (법인세법 시행규칙 별지 서식 항목 기준)."""
    v = repo.get("ga_vehicles", vid)
    logs = sorted(repo.vehicle_logs(vid, year), key=lambda x: (x["drive_date"], x["km_start"]))
    out = [[f"업무용승용차 운행기록부 ({year}년)", f"차량번호 {v['plate_no']}", f"차종 {v['model']}"],
           ["사용일자", "부서", "성명", "주행 전 계기판 거리(km)", "주행 후 계기판 거리(km)", "주행거리(km)",
            "출·퇴근용(km)", "일반 업무용(km)", "비업무용(km)", "비고"]]
    for l in logs:
        d = l["distance"]
        out.append([l["drive_date"], l["driver_dept"], l["driver_name"], l["km_start"], l["km_end"], d,
                    d if l["purpose"] == "출퇴근" else 0, d if l["purpose"] == "일반업무" else 0,
                    d if l["purpose"] == "비업무" else 0,
                    " ".join(x for x in ["[목적 확인 필요]" if l["purpose"] == PURPOSE_PENDING else "", l["note"]] if x)])
    st = services.vehicle_stats(vid, year)
    out.append(["합계", "", "", "", "", st["total_km"], "", "", "",
                f"업무사용비율 {st['biz_ratio']:.1f}%" if st["biz_ratio"] is not None else ""])
    audit("ga", "운행기록부 내보내기", v["plate_no"], str(year))
    return _csv_response(out, f"운행기록부_{v['plate_no']}_{year}.csv")


# ── 3. 문서수발·관인 ────────────────────────────────────────────────────
@router.get("/docs", response_class=HTMLResponse)
def docs(request: Request, tab: str = "docs", direction: str = "", year: str = "", q: str = "", status: str = ""):
    y = year or str(today().year)
    rows = repo.list_docs(direction, y, q, status)
    for d in rows:
        d["due_dday"] = (to_date(d["due_date"]) - today()).days if to_date(d["due_date"]) else None
    seals = repo.list_seals(y)
    prefix = get_settings().get("doc_prefix", "총무")
    return _render(request, "ga/docs.html", {
        "tab": tab, "rows": rows, "seals": seals, "f": {"direction": direction, "year": y, "q": q, "status": status},
        "methods": get_codes("DOC_METHOD"), "statuses": DOC_STATUSES, "retention": get_codes("RETENTION"),
        "seal_types": get_codes("SEAL_TYPE"),
        "seal_statuses": SEAL_STATUSES, "depts": get_codes("DEPT"),
        "next_out": repo.next_doc_no("발신", None, prefix), "next_in": repo.next_doc_no("수신", None, prefix),
        "years": sorted({str(today().year)} | {d["doc_date"][:4] for d in repo.list_docs()}, reverse=True),
        "seal_stat": {s: sum(1 for x in seals if x["status"] == s) for s in SEAL_STATUSES},
    }, "ga.docs")


@router.post("/docs/save")
async def doc_save(request: Request):
    f = dict(await request.form())
    rid = int(f.get("id") or 0) or None
    if not rid:
        f["doc_no"] = repo.next_doc_no(f.get("direction", "수신"), f.get("doc_date"), get_settings().get("doc_prefix", "총무"))
    else:
        f.pop("doc_no", None)
        f.pop("direction", None)
    repo.save("ga_docs", rid, f)
    return _redirect("/ga/docs", f"{f.get('doc_no', '')} 저장되었습니다".strip())


@router.post("/seals/{sid}/decide")
async def seal_decide(request: Request, sid: int):
    """관인 사용 승인/반려 — 시스템관리자만 (미들웨어에서 권한 확인)."""
    f = dict(await request.form())
    decision = f.get("decision")
    if decision not in ("승인", "반려", "사용완료"):
        raise HTTPException(400)
    execute("UPDATE ga_seal_logs SET status=?, approver=?, approved_at=? WHERE id=?",
            (decision, request.state.user["display_name"], datetime.now().strftime("%Y-%m-%d %H:%M"), sid))
    audit("ga", f"관인 사용 {decision}", f"#{sid}", "")
    return _redirect("/ga/docs?tab=seal", f"{decision} 처리되었습니다")


# ── 4. 계약·임대차 ──────────────────────────────────────────────────────
@router.get("/contracts", response_class=HTMLResponse)
def contracts(request: Request, category: str = "", status: str = "유효"):
    services.roll_auto_renew()
    rows = [services.contract_view(c) for c in repo.list_contracts(category, status)]
    active = [services.contract_view(c) for c in repo.list_contracts(status="유효")]
    return _render(request, "ga/contracts.html", {
        "rows": rows, "f": {"category": category, "status": status}, "categories": get_codes("GA_CONTRACT"),
        "cycles": PAY_CYCLES, "statuses": CONTRACT_STATUSES,
        "sum": {"count": len(active), "monthly": sum(c["monthly"] for c in active),
                "yearly": sum(c["monthly"] for c in active) * 12, "deposit": sum(c["deposit"] or 0 for c in active),
                "soon": sum(1 for c in active if c["phase"] in ("만료임박", "통보임박", "통보경과"))},
    }, "ga.contracts")


@router.get("/contracts/export.csv")
def contracts_export():
    rows = [services.contract_view(c) for c in repo.list_contracts()]
    out = [["구분", "계약명", "상대방", "계약번호", "시작일", "종료일", "금액", "부가세구분", "공급가액", "부가세", "주기", "월환산", "보증금", "자동갱신",
            "통보기한", "상태", "담당"]]
    for c in rows:
        out.append([c["category"], c["title"], c["counterparty"], c["contract_no"], c["start_date"], c["end_date"],
                    c["amount"], c["amount_vat"], vat_split(c["amount"], c["amount_vat"])["supply"],
                    vat_split(c["amount"], c["amount_vat"])["vat"], c["pay_cycle"], round(c["monthly"]), c["deposit"], "Y" if c["auto_renew"] else "N",
                    c["notice_date"], c["phase"], c["manager_name"]])
    return _csv_response(out, f"계약현황_{today().isoformat()}.csv")


# ── 5. 경조사 ───────────────────────────────────────────────────────────
@router.get("/events", response_class=HTMLResponse)
def events(request: Request, year: str = "", status: str = ""):
    y = year or str(today().year)
    rows = repo.list_events(y, status)
    valid = [e for e in rows if e["status"] != "취소"]
    by_cat = {}
    for e in valid:
        by_cat.setdefault(e["category"], {"count": 0, "amount": 0})
        by_cat[e["category"]]["count"] += 1
        by_cat[e["category"]]["amount"] += e["amount"] or 0
    return _render(request, "ga/events.html", {
        "rows": rows, "rules": repo.event_rules(), "f": {"year": y, "status": status},
        "categories": get_codes("EVENT_CATEGORY"), "relations": get_codes("EVENT_RELATION"), "statuses": EVENT_STATUSES,
        "sum": {"count": len(valid), "amount": sum(e["amount"] or 0 for e in valid),
                "pending": sum(1 for e in rows if e["status"] == "신청"),
                "leave": sum(e["leave_days"] or 0 for e in valid)},
        "by_cat": by_cat,
        "years": sorted({str(today().year)} | {e["event_date"][:4] for e in repo.list_events()}, reverse=True),
    }, "ga.events")


@router.post("/events/save")
async def event_save(request: Request):
    f = dict(await request.form())
    rule = next((r for r in repo.event_rules() if r["category"] == f.get("category") and r["relation"] == f.get("relation")), None)
    if rule:
        for k in ("amount", "leave_days", "flower"):
            if f.get(k) in (None, ""):
                f[k] = rule[k]
    f.setdefault("flower", 0)
    if f.get("status") == "지급완료" and not f.get("paid_date"):
        f["paid_date"] = today().isoformat()
    repo.save("ga_events", int(f.get("id") or 0) or None, f)
    return _redirect("/ga/events", "저장되었습니다")


# ── 공통 저장·삭제 (차량·정비·관인·계약·규정) ─────────────────────────────
@router.post("/{kind}/save")
async def generic_save(request: Request, kind: str):
    table = _table(kind)
    f = dict(await request.form())
    for cb in ("auto_renew", "flower"):
        if table in ("ga_contracts", "ga_event_rules") and cb in ("auto_renew", "flower"):
            f.setdefault(cb, 0)
    if table == "ga_seal_logs" and not f.get("id"):
        f["status"] = "신청"
    rid = repo.save(table, int(f.get("id") or 0) or None, f)
    nxt = f.get("next") or {"vehicles": f"/ga/vehicles/{rid}", "maint": f"/ga/vehicles/{f.get('vehicle_id')}",
                            "seals": "/ga/docs?tab=seal", "contracts": "/ga/contracts", "rules": "/ga/events"}.get(kind, "/ga/dashboard")
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/dashboard", "저장되었습니다")


@router.post("/{kind}/{rid}/delete")
async def generic_delete(request: Request, kind: str, rid: int):
    f = dict(await request.form())
    if kind == "vlogs" and repo.is_cartax_log(rid):
        return _redirect(f.get("next") or "/ga/vehicles", CARTAX_READONLY)
    repo.delete(_table(kind), rid)
    if kind == "assets":
        photos.delete(rid)
    nxt = f.get("next") or "/ga/dashboard"
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/dashboard", "삭제되었습니다")
