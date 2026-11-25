"""총무관리 페이지·API 라우트 (prefix: /ga)."""
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import trash
from app.common import templates, today, to_date, vat_split
from app.config import settings as cfg
from app.db import audit, get_codes, get_settings, execute, dept_text, query
from app.modules.ga import cartax, contract_intake, fleet, photos, repo, services
from app.modules.ga.constants import (
    ASSET_CATEGORIES, ASSET_STATUSES, VEHICLE_STATUSES, DRIVE_PURPOSES, PURPOSE_PENDING, DOC_STATUSES, SEAL_STATUSES,
    PAY_CYCLES, CONTRACT_STATUSES, EVENT_STATUSES, VAT_TYPES,
)
from app.modules.hr.routes import _csv_response, _redirect

router = APIRouter(prefix="/ga")
public_router = APIRouter()      # 로그인 없이 열리는 화면 (자산 라벨 QR)

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
    public = _public_url()
    for a in rows:
        a["qr_text"] = _qr_text(a, public)
    return _render(request, "ga/asset_labels.html", {"rows": rows, "public_url": public}, "ga.assets")


def _qr_text(a: dict, public: str) -> str:
    """라벨 QR 내용: 직원 접속 주소가 있으면 로그인 없이 기본 정보를 보는 주소, 없으면 자산번호."""
    return f"{public}/q/{repo.asset_qr_token(a['id'])}" if public else a["asset_no"]


def _mask_name(name: str | None) -> str:
    if not name:
        return ""
    return name[0] + "*" if len(name) == 2 else name[0] + "*" * (len(name) - 2) + name[-1]


@public_router.get("/q/{token}", response_class=HTMLResponse)
def asset_public(request: Request, token: str):
    """자산 라벨 QR: 로그인 없이 기본 정보만 (금액·시리얼·이력·사진 제외, 사용자 이름은 가림)."""
    a = repo.asset_by_qr(token)
    if not a:
        return templates.TemplateResponse(request, "ga/asset_public.html", {"a": None}, status_code=404)
    a["holder_masked"] = _mask_name(a["holder_name"])
    return templates.TemplateResponse(request, "ga/asset_public.html",
                                      {"a": a, "logged_in": bool(request.state.user)})


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
        "qr_text": _qr_text(a, public), "qr_is_url": bool(public),
        "categories": ASSET_CATEGORIES, "statuses": ASSET_STATUSES, "locations": get_codes("ASSET_LOC"),
        "fp": _files_panel(request, "ga.assets", aid, f"/ga/assets/{aid}"),
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
    return _redirect(f"/ga/assets/{aid}", "이력을 휴지통으로 옮겼습니다. 자산 상태도 다시 계산했습니다")


# ── 2. 법인차량 ─────────────────────────────────────────────────────────
def _fleet_opts() -> dict:
    return {"insp_kinds": fleet.KINDS, "damage_kinds": fleet.DAMAGE_KINDS, "damage_statuses": fleet.DAMAGE_STATUSES,
            "insurance_opts": fleet.INSURANCE, "resp_opts": fleet.RESPONSIBILITY, "fine_kinds": fleet.FINE_KINDS,
            "fine_statuses": fleet.FINE_STATUSES}


@router.get("/vehicles", response_class=HTMLResponse)
def vehicles(request: Request, tab: str = "list", year: int | None = None, status: str = ""):
    if tab in ("fines", "damages") and request.state.user["role"] not in ("admin", "hr", "ga"):
        tab = "list"                                   # 직원별 과태료·사고 책임은 총무 담당자 이상만
    rows = repo.list_vehicles()
    users = fleet.current_users()
    for v in rows:
        v["stat"] = services.vehicle_stats(v["id"])
        v["odometer"] = repo.last_odometer(v["id"])
        v["user"] = users.get(v["id"])
        for k in ("insurance_end", "inspection_due", "lease_end"):
            v[k + "_dday"] = (to_date(v[k]) - today()).days if to_date(v[k]) else None
    year = year or today().year
    tab = tab if tab in ("list", "inspections", "damages", "fines", "cost") else "list"
    extra = {"tab": tab, "year": year, "status": status, **_fleet_opts(),
             "veh_opts": [{"v": v["id"], "t": f"{v['plate_no']} {v['model']}"} for v in rows if v["status"] != "매각"],
             "fleet_alerts": [a for a in services.build_alerts([], rows, [], 0) if a["category"] in services.VEHICLE_ALERT_CATS],
             "periodic_due": fleet.periodic_due()}
    if tab == "inspections":
        extra["insp_rows"] = fleet.inspections(status=status)
    elif tab == "damages":
        extra["damage_rows"] = fleet.damages(status=status, year="" if status == "진행" else str(year))
    elif tab == "fines":
        extra["fine_rows"] = fleet.fines(status=status, year="" if status == "진행" else str(year))
    elif tab == "cost":
        extra["cost"] = fleet.cost_table(year)
    return _render(request, "ga/vehicles.html", {**extra,
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
        "cur_user": fleet.current_user(vid), "assign_rows": fleet.assign_history(vid)[:10],
        "insp_rows": fleet.inspections(vid)[:10], "damage_rows": fleet.damages(vid)[:10], "fine_rows": fleet.fines(vid)[:10],
        "oil": fleet.oil_status(v), "oil_interval": int(v.get("oil_interval_km") or fleet.OIL_DEFAULT_KM),
        "veh_opts": [{"v": v["id"], "t": f"{v['plate_no']} {v['model']}"}], **_fleet_opts(),
        "fp": _files_panel(request, "ga.vehicles", vid, f"/ga/vehicles/{vid}"),
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
        "seal_statuses": SEAL_STATUSES, "depts": [{"v": d, "t": dept_text(d)} for d in get_codes("DEPT")],
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
ITEM_STATUS = {"유효": "관리중", "": ""}          # 화면의 상태 고르기 → 만기 항목 쪽 조건 ('유효' = 유효 + 갱신중)


@router.get("/contracts", response_class=HTMLResponse)
def contracts(request: Request, category: str = "", status: str = "유효", kind: str = ""):
    """계약·만기 관리: 계약·임대차와 면허·보험·인증 등 회사 관리 만기를 한 표로 (만기 가까운 순)."""
    from app.modules.ga import company as items_mod
    from app.modules.mgmt.constants import COMPANY_CYCLES, COMPANY_EXAMPLES, COMPANY_STATUSES
    COMPANY_CATEGORIES = items_mod.categories()
    services.roll_auto_renew()
    contract_cats = get_codes("GA_CONTRACT")
    show_c = kind in ("", "contract") and (not category or category in contract_cats) and (status in CONTRACT_STATUSES or status == "")
    show_i = kind in ("", "item") and (not category or category in COMPANY_CATEGORIES) and (status in COMPANY_STATUSES or status == "")
    rows = []
    if show_c:
        for c in repo.list_contracts(category, status):
            c = services.contract_view(c)
            rows.append({"kind": "contract", "dday": c["dday"], "c": c})
    item_rows = items_mod.items(category, ITEM_STATUS.get(status, status)) if show_i else []
    for r in item_rows:
        nx = items_mod.next_expire(r)
        rows.append({"kind": "item", "dday": r["dday"], "r": r, "next": nx.isoformat() if nx else ""})
    rows.sort(key=lambda x: (x["dday"] is None, x["dday"] if x["dday"] is not None else 0))
    active = [services.contract_view(c) for c in repo.list_contracts(status="유효")]
    managed = items_mod.items(status="관리중")
    can_write = request.state.user["role"] in ("admin", "hr", "ga")
    drafts = []
    drafts_on = bool(cfg.contract_root) and cfg.contract_drafts
    if can_write and drafts_on:
        for d in contract_intake.list_drafts("확인필요"):
            data = d["data"] or {}
            fill = {"status": "유효", "pay_cycle": "월", "notice_days": 30, "amount_vat": "부가세별도",
                    **data.get("fields", {}), "draft_id": d["id"]}
            drafts.append({**d, "fields": data.get("fields", {}), "checks": data.get("checks", []),
                           "files": data.get("files", []), "fill": fill})
    return _render(request, "ga/contracts.html", {
        "drafts": drafts, "intake": {**contract_intake.state, "enabled": drafts_on},
        "rows": rows, "f": {"category": category, "status": status, "kind": kind}, **_contract_form_ctx(),
        "history": items_mod.history() if show_i else [],
        "status_opts": ["유효", "갱신중", "만료", "해지", "종료", ""],
        "sum": {"count": len(active), "monthly": sum(c["monthly"] for c in active),
                "yearly": sum(c["monthly"] for c in active) * 12, "deposit": sum(c["deposit"] or 0 for c in active),
                "soon": sum(1 for c in active if c["phase"] in ("만료임박", "통보임박", "통보경과")),
                "items": len(managed), "item_alerts": sum(1 for r in managed if r["alert"])},
    }, "ga.contracts")


@router.post("/contract-drafts/scan")
def contract_drafts_scan():
    return _redirect("/ga/contracts", contract_intake.scan_once(force=True)["message"])


def _pending_draft(did: int) -> dict | None:
    d = contract_intake.get_draft(did)
    return d if d and d["status"] == "확인필요" else None


@router.post("/contract-drafts/confirm")
async def contract_draft_confirm(request: Request):
    """초안 확인 → 계약 등록. 폴더 이름을 계약에 남겨 서류를 계속 열어 볼 수 있게 합니다."""
    f = dict(await request.form())
    d = _pending_draft(int(f.get("draft_id") or 0))
    if not d:
        return _redirect("/ga/contracts", "이미 처리되었거나 삭제된 초안입니다")
    if not (f.get("title") or "").strip() or not (f.get("counterparty") or "").strip():
        return _redirect("/ga/contracts", "계약명과 상대방은 꼭 입력해 주세요")
    f.setdefault("auto_renew", 0)
    f["folder_name"] = d["folder_name"]
    f.pop("id", None)
    rid = repo.save("ga_contracts", None, f)
    contract_intake.update_draft(d["id"], status="등록완료", contract_id=rid)
    audit("ga", "계약 초안 등록", d["folder_name"], f"계약 #{rid}")
    return _redirect("/ga/contracts", f"'{f['title']}' 계약을 등록했습니다")


@router.post("/contract-drafts/{did}/delete")
def contract_draft_delete(did: int):
    d = _pending_draft(did)
    if not d:
        return _redirect("/ga/contracts", "이미 처리된 초안입니다")
    contract_intake.update_draft(did, status="삭제", data={})
    audit("ga", "계약 초안 삭제", d["folder_name"], "")
    return _redirect("/ga/contracts", f"{d['folder_name']} 초안을 삭제했습니다. 폴더에 서류를 새로 넣거나 바꾸면 다시 만들어집니다")


@router.post("/contract-drafts/{did}/exclude")
def contract_draft_exclude(did: int):
    d = _pending_draft(did)
    if not d:
        return _redirect("/ga/contracts", "이미 처리된 초안입니다")
    contract_intake.update_draft(did, status="제외", data={})
    audit("ga", "계약 초안 제외", d["folder_name"], "")
    return _redirect("/ga/contracts", f"{d['folder_name']} 폴더는 앞으로 초안을 만들지 않습니다")


def _folder_file(folder_name: str, name: str):
    """폴더 안에 실제로 있는 파일만 열어 줍니다 (경로 조작 차단)."""
    path = contract_intake.folder_path(folder_name)
    if not path or name not in contract_intake.list_files(folder_name):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    audit("ga", "계약 서류 열람", folder_name, name)
    ext = name.rsplit(".", 1)[-1].lower()
    media = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext)
    return FileResponse(os.path.join(path, name), filename=name, media_type=media or "application/octet-stream",
                        content_disposition_type="inline" if media else "attachment")


@router.get("/contract-drafts/{did}/file")
def contract_draft_file(did: int, name: str):
    d = contract_intake.get_draft(did)
    if not d:
        raise HTTPException(404, "초안을 찾을 수 없습니다")
    return _folder_file(d["folder_name"], name)


@router.get("/contracts/{cid}/file")
def contract_file(cid: int, name: str):
    c = repo.get("ga_contracts", cid)
    if not c or not c.get("folder_name"):
        raise HTTPException(404, "서류 폴더가 연결되지 않은 계약입니다")
    return _folder_file(c["folder_name"], name)


def _files_panel(request: Request, menu: str, ref_id: int, back: str) -> dict | None:
    """상세 화면 아래에 넣는 서류 묶음. 서류를 볼 수 있는 권한(총무 담당자 이상)일 때만."""
    from app import archive, files
    if not archive.allowed(menu, request.state.user["role"]):
        return None
    try:
        return files.panel(menu, ref_id, back)
    except Exception:                                   # noqa: BLE001
        return None


def _contract_form_ctx() -> dict:
    """계약·면허 등록 창(templates/ga/_contract_modals.html)에 필요한 고르기 값."""
    from app.modules.ga import company as items_mod
    from app.modules.mgmt.constants import COMPANY_CYCLES, COMPANY_EXAMPLES, COMPANY_STATUSES
    staff = repo.ga_staff()
    return {"staff_mails": [e for e in staff if e["mail"]], "categories": get_codes("GA_CONTRACT"), "item_categories": items_mod.categories(), "item_cycles": COMPANY_CYCLES,
            "item_statuses": COMPANY_STATUSES, "examples": COMPANY_EXAMPLES, "cycles": PAY_CYCLES, "statuses": CONTRACT_STATUSES}


HISTORY_WORDS = {"계약 등록": "등록", "계약 수정": "내용 고침", "계약 만기 알림 메일": "만기 알림 메일 보냄", "회사 관리 만기 저장": "등록·내용 고침",
                 "회사 관리 갱신": "갱신", "만기 알림 메일": "만기 알림 메일 보냄", "서류 붙임": "서류 올림", "폴더 파일을 대장에 올림": "폴더 파일을 대장에 올림",
                 "서류 삭제": "서류 지움(삭제함으로 옮김)"}


def _history(where: str, params: tuple) -> list[dict]:
    """접속기록에서 이 항목에 한 일만 추려 최근 순으로. (누가·언제·무엇을)"""
    rows = query("SELECT created_at, username, action, target, detail FROM audit_logs WHERE " + where + " ORDER BY id DESC LIMIT 60", params)
    for r in rows:
        r["what"] = HISTORY_WORDS.get(r["action"], r["action"])
        if r["action"] == "서류 삭제":
            r["detail"] = r["target"]
    return rows


@router.get("/contracts/item/{iid:int}", response_class=HTMLResponse)
def item_detail(request: Request, iid: int):
    """면허·보험·인증 등 한 건: 지금 상태 · 이력 · 서류."""
    from app import archive
    from app.modules.ga import company as items_mod
    r = next((x for x in items_mod.items() if x["id"] == iid), None) or items_mod.get(iid)
    if not r:
        raise HTTPException(404, "항목을 찾을 수 없습니다")
    r.setdefault("dday", None)
    r.setdefault("alert", False)
    nx = items_mod.next_expire(r)
    t = archive.target("ga.company", iid) or {"title": r["name"]}
    hist = _history("(module='ga' AND action IN ('회사 관리 만기 저장','회사 관리 갱신','만기 알림 메일') AND target=?) "
                    "OR (module='files' AND target=?) OR (module='files' AND detail LIKE ?)",
                    (r["name"], f"{archive.MENUS['ga.company'][1]} {t['title']}"[:120], f"ga.company #{iid} %"))
    back = f"/ga/contracts/item/{iid}"
    return _render(request, "ga/contract_detail.html", {
        "kind": "item", "r": r, "next_expire": nx.isoformat() if nx else "", "renewals": items_mod.history(iid), "hist": hist,
        "fp": _files_panel(request, "ga.company", iid, back), "modal_next": back, **_contract_form_ctx(),
    }, "ga.contracts")


@router.get("/contracts/{cid:int}", response_class=HTMLResponse)
def contract_detail(request: Request, cid: int):
    """계약 한 건: 지금 상태 · 이력 · 서류."""
    from app import archive
    c = next((x for x in repo.list_contracts() if x["id"] == cid), None)
    if not c:
        raise HTTPException(404, "계약을 찾을 수 없습니다")
    c = services.contract_view(c)
    at = archive.target("ga.contracts", cid) or {}
    fkey = f"{archive.MENUS['ga.contracts'][1]} {at['target'] if at.get('dup') else c['title']} ("[:120]   # 서류 기록의 대상 이름 앞부분 (상대방 이름이 바뀌어도 찾게)
    hist = _history("(module='ga' AND action IN ('계약 등록','계약 수정') AND target=?) OR (module='ga' AND action='계약 만기 알림 메일' AND target=?) "
                    "OR (module='files' AND substr(target, 1, ?)=?) OR (module='files' AND detail LIKE ?)",
                    (f"#{cid}", c["title"], len(fkey), fkey, f"ga.contracts #{cid} %"))
    first = next((n for n, h in enumerate(hist) if h["action"] == "계약 등록"), None)
    if first is not None:                                    # 같은 번호를 쓰던 예전 자료의 기록은 빼고, 이 계약을 등록한 때부터
        hist = hist[:first + 1]
    back = f"/ga/contracts/{cid}"
    return _render(request, "ga/contract_detail.html", {
        "kind": "contract", "c": c, "hist": hist, "fp": _files_panel(request, "ga.contracts", cid, back), "modal_next": back,
        **_contract_form_ctx(),
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
    form = await request.form()
    f = dict(form)
    uploads = [u for u in form.getlist("files") if getattr(u, "filename", "")][:20] if kind == "contracts" else []
    f.pop("files", None)
    for cb in ("auto_renew", "flower"):
        if table in ("ga_contracts", "ga_event_rules") and cb in ("auto_renew", "flower"):
            f.setdefault(cb, 0)
    if table == "ga_seal_logs":
        # 승인·반려는 관리자 전용 경로(/ga/seals/{id}/decide)에서만. 여기서는 상태·승인자·승인일을 받지 않음
        for k in ("status", "approver", "approved_at"):
            f.pop(k, None)
        if f.get("id"):
            cur = repo.get("ga_seal_logs", int(f["id"]))
            if not cur:
                raise HTTPException(404)
            if cur["status"] != "신청":
                return _redirect("/ga/docs?tab=seal", "이미 처리된 관인 신청은 고칠 수 없습니다")
        else:
            f["status"] = "신청"
    if table == "ga_contracts":                              # 만기 알림 받을 메일: 주소 모양 확인 후 쉼표로 정리
        from app.modules.ga import company as items_mod
        try:
            old = repo.get("ga_contracts", int(f["id"])) if f.get("id") else None
            f["alert_email"] = items_mod.allowed_mails(f.get("alert_email"), (old or {}).get("alert_email") or "")
        except items_mod.CompanyError as ex:
            return _redirect("/ga/contracts", str(ex))
    rid = repo.save(table, int(f.get("id") or 0) or None, f)
    extra = ""
    if kind in ("vehicles", "contracts"):                    # 문서 보관함에 그 대상의 폴더를 먼저 만들어 둠 (실패해도 저장은 그대로)
        from app import archive
        await run_in_threadpool(archive.ensure_quiet, "ga." + kind, rid)
        extra = await archive.save_uploads("ga." + kind, rid, uploads)
        if kind == "contracts" and not f.get("id"):
            extra += archive.dup_note("ga.contracts", rid)
    nxt = f.get("next") or {"vehicles": f"/ga/vehicles/{rid}", "maint": f"/ga/vehicles/{f.get('vehicle_id')}",
                            "seals": "/ga/docs?tab=seal", "contracts": "/ga/contracts", "rules": "/ga/events"}.get(kind, "/ga/dashboard")
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/dashboard", "저장되었습니다" + extra)


@router.post("/{kind}/{rid}/delete")
async def generic_delete(request: Request, kind: str, rid: int):
    f = dict(await request.form())
    if kind == "vlogs" and repo.is_cartax_log(rid):
        return _redirect(f.get("next") or "/ga/vehicles", CARTAX_READONLY)
    repo.delete(_table(kind), rid)
    nxt = f.get("next") or "/ga/dashboard"
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/dashboard", trash.MOVED)
