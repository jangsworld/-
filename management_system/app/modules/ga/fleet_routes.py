"""법인차량 관리 강화 화면: 사용자 이력, 사진 점검·비교, 사고·파손, 과태료, 차량 QR.

- /ga/... : 총무 담당자 화면 (로그인)
- /vq/{열쇠} : 차에 붙인 QR. 직원은 이름+생년월일로 본인 확인 후 점검 사진·파손 신고·인수인계 확인
"""
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.common import templates, today
from app.config import settings as cfg
from app.db import audit, get_settings, query_one
from app.modules.ga import fleet, photos, repo
from app.modules.hr.routes import _redirect

router = APIRouter(prefix="/ga")
public_router = APIRouter(prefix="/vq")
COOKIE = "vq_s"
STAFF = ("admin", "hr", "ga")


def _vehicle(vid: int) -> dict:
    v = repo.get("ga_vehicles", vid)
    if not v:
        raise HTTPException(404, "차량을 찾을 수 없습니다")
    return v


def _insp(vid: int, iid: int) -> dict:
    i = fleet.get_inspection(iid)
    if not i or i["vehicle_id"] != vid:
        raise HTTPException(404, "점검을 찾을 수 없습니다")
    return i


def _who(request: Request) -> str:
    u = request.state.user
    return f"{u['display_name']}(관리부)" if u else ""


def _base_url(request: Request) -> str:
    return (get_settings().get("public_url") or cfg.public_url or str(request.base_url)).rstrip("/")


def _shoot_ctx(i: dict, photo_url, post_base: str, back: str, viewer: dict | None = None) -> dict:
    base = fleet.get_inspection(i["prev_id"]) if i.get("prev_id") else None
    rows = fleet.compare_rows(i)
    for r in rows:
        for k in ("now", "before"):
            if r[k]:
                r[k]["thumb"] = photo_url(r[k]["id"], True)
                r[k]["url"] = photo_url(r[k]["id"], False)
    dmg = [{**p, "thumb": photo_url(p["id"], True), "url": photo_url(p["id"], False)}
           for p in fleet.photo_rows(i["id"]) if p["slot"] == "damage"]
    sides = []
    if viewer and i["status"] == "확인대기":
        if viewer["id"] == i["from_emp_id"] and not i["from_ok_at"]:
            sides.append(("from", "인계자"))
        if viewer["id"] == i["to_emp_id"] and not i["to_ok_at"]:
            sides.append(("to", "인수자"))
    return {"i": i, "rows": rows, "damage": dmg, "base": base, "post": post_base, "back": back,
            "missing": fleet.missing_slots(i["id"]), "fuel_levels": ["가득", "3/4", "1/2", "1/4", "부족"],
            "confirm_sides": sides, "viewer": viewer, "report": i["kind"] == fleet.REPORT_KIND}


async def _upload(request: Request, iid: int, by: str) -> JSONResponse:
    form = await request.form()
    up = form.get("file")
    if not getattr(up, "filename", ""):
        return JSONResponse({"ok": False, "message": "사진을 선택하세요"}, status_code=400)
    raw = await up.read()
    p, err = await run_in_threadpool(fleet.add_photo, iid, str(form.get("slot", "")), raw, str(form.get("note", "")), by)
    if err:
        return JSONResponse({"ok": False, "message": err}, status_code=400)
    return JSONResponse({"ok": True, "id": p["id"], "size": p["size"], "missing": fleet.missing_slots(iid)})


# ════════════════════════════════════════════════════════════════════════
# 총무 담당자 화면
# ════════════════════════════════════════════════════════════════════════
@router.get("/vehicles/labels", response_class=HTMLResponse)
def vehicle_labels(request: Request, ids: str = ""):
    sel = {int(x) for x in ids.split(",") if x.strip().isdigit()}
    base = _base_url(request)
    rows = [v for v in repo.list_vehicles() if v["status"] != "매각" and (not sel or v["id"] in sel)]
    for v in rows:
        v["qr_text"] = f"{base}/vq/{fleet.qr_token(v['id'])}"
    return templates.TemplateResponse(request, "ga/vehicle_labels.html", {
        "rows": rows, "base": base, "active_menu": "ga.vehicles",
        "public_set": bool(get_settings().get("public_url") or cfg.public_url)})


@router.post("/vehicles/{vid}/assign")
async def vehicle_assign(request: Request, vid: int):
    _vehicle(vid)
    f = await request.form()
    emp = fleet._int(f.get("employee_id")) or None
    fleet.set_user(vid, emp, f.get("on_date") or today().isoformat(), (f.get("note") or "")[:200], None, _who(request))
    return _redirect(f"/ga/vehicles/{vid}", "사용자를 바꿨습니다" if emp else "반납(사용자 없음)으로 바꿨습니다")


@router.post("/vehicles/{vid}/oil")
async def vehicle_oil(request: Request, vid: int):
    _vehicle(vid)
    f = await request.form()
    km = max(1000, min(50000, fleet._int(f.get("oil_interval_km"), fleet.OIL_DEFAULT_KM)))
    repo.execute("UPDATE ga_vehicles SET oil_interval_km=? WHERE id=?", (str(km), vid))
    return _redirect(f"/ga/vehicles/{vid}", f"엔진오일 교체 주기를 {km:,}km로 정했습니다")


@router.post("/vehicles/{vid}/inspections")
async def inspection_new(request: Request, vid: int):
    _vehicle(vid)
    f = await request.form()
    iid, err = fleet.create_inspection(vid, f.get("kind", ""), f.get("insp_date", ""), f.get("from_emp_id"),
                                       f.get("to_emp_id"), f.get("note", ""), _who(request))
    if err:
        return _redirect(f"/ga/vehicles/{vid}", err)
    return _redirect(f"/ga/vehicles/{vid}/inspections/{iid}", "점검을 만들었습니다. 사진을 찍어 주세요")


@router.get("/vehicles/{vid}/inspections/{iid}", response_class=HTMLResponse)
def inspection_page(request: Request, vid: int, iid: int):
    i = _insp(vid, iid)
    ctx = _shoot_ctx(i, lambda pid, t: f"/ga/vehicle-photos/{pid}{'?thumb=1' if t else ''}",
                     f"/ga/vehicles/{vid}/inspections/{iid}", f"/ga/vehicles/{vid}")
    start = ctx["base"]["insp_date"] if ctx["base"] else None
    ctx.update({
        "v": _vehicle(vid), "candidates": fleet.drivers_between(vid, start, i["insp_date"]),
        "known": fleet.known_damages(vid), "verdicts": fleet.VERDICTS, "qr_url": f"{_base_url(request)}/vq/{fleet.qr_token(vid)}",
        "msg": request.query_params.get("msg", ""), "active_menu": "ga.vehicles",
        "can_write": request.state.user["role"] in STAFF, "linked_damages": [d for d in fleet.damages(vid) if d["inspection_id"] == iid],
    })
    return templates.TemplateResponse(request, "ga/vehicle_inspection.html", ctx)


@router.get("/vehicles/{vid}/inspections/{iid}/shoot", response_class=HTMLResponse)
def inspection_shoot(request: Request, vid: int, iid: int):
    i = _insp(vid, iid)
    ctx = _shoot_ctx(i, lambda pid, t: f"/ga/vehicle-photos/{pid}{'?thumb=1' if t else ''}",
                     f"/ga/vehicles/{vid}/inspections/{iid}", f"/ga/vehicles/{vid}/inspections/{iid}")
    ctx["staff"] = True
    return templates.TemplateResponse(request, "ga/vehicle_shoot.html", ctx)


@router.post("/vehicles/{vid}/inspections/{iid}/photo")
async def inspection_photo(request: Request, vid: int, iid: int):
    _insp(vid, iid)
    return await _upload(request, iid, _who(request))


@router.post("/vehicles/{vid}/inspections/{iid}/photo/{pid}/delete")
def inspection_photo_delete(request: Request, vid: int, iid: int, pid: int):
    _insp(vid, iid)
    err = fleet.delete_photo(iid, pid, _who(request))
    return JSONResponse({"ok": not err, "message": err}, status_code=400 if err else 200)


@router.post("/vehicles/{vid}/inspections/{iid}/meta")
async def inspection_meta(request: Request, vid: int, iid: int):
    _insp(vid, iid)
    f = await request.form()
    fleet.save_meta(iid, f.get("odometer"), f.get("fuel_level", ""), f.get("note"))
    return JSONResponse({"ok": True})


@router.post("/vehicles/{vid}/inspections/{iid}/finish")
def inspection_finish(request: Request, vid: int, iid: int):
    _insp(vid, iid)
    err = fleet.finish_shooting(iid, _who(request))
    return _redirect(f"/ga/vehicles/{vid}/inspections/{iid}", err or "촬영을 마쳤습니다. 이전 사진과 비교해 확정하세요")


@router.post("/vehicles/{vid}/inspections/{iid}/confirm")
async def inspection_confirm(request: Request, vid: int, iid: int):
    """관리부 입회 확인 (당사자가 휴대폰으로 확인하지 못한 경우)."""
    i = _insp(vid, iid)
    side = (await request.form()).get("side", "")
    name = i["from_name"] if side == "from" else i["to_name"]
    err = fleet.confirm_party(iid, side, f"{name or ''} — 관리부 {request.state.user['display_name']} 입회")
    return _redirect(f"/ga/vehicles/{vid}/inspections/{iid}", err or "확인을 기록했습니다")


@router.post("/vehicles/{vid}/inspections/{iid}/finalize")
async def inspection_finalize(request: Request, vid: int, iid: int):
    _insp(vid, iid)
    f = await request.form()
    verdicts = {int(k[8:]): v for k, v in f.items() if k.startswith("verdict_") and k[8:].isdigit()}
    notes = {int(k[5:]): v for k, v in f.items() if k.startswith("note_") and k[5:].isdigit()}
    err, created = fleet.finalize(iid, verdicts, notes, f.get("responsible_id"), request.state.user["display_name"])
    if err:
        return _redirect(f"/ga/vehicles/{vid}/inspections/{iid}", err)
    msg = "점검을 확정했습니다" + (f". 새 파손 {len(created)}건을 사고·파손 기록에 올렸습니다 — 수리비·책임을 정해 주세요" if created else " (이상 없음)")
    return _redirect(f"/ga/vehicles/{vid}/inspections/{iid}", msg)


@router.post("/vehicles/{vid}/inspections/{iid}/delete")
def inspection_delete(request: Request, vid: int, iid: int):
    _insp(vid, iid)
    err = fleet.delete_inspection(iid, request.state.user["display_name"])
    return _redirect(f"/ga/vehicles/{vid}" if not err else f"/ga/vehicles/{vid}/inspections/{iid}", err or "점검을 삭제했습니다")


@router.get("/vehicle-photos/{pid}")
def vehicle_photo(pid: int, thumb: bool = False):
    p = fleet.photo_belongs(pid)
    path = photos.vehicle_path(p["inspection_id"], pid, thumb) if p else ""
    if not p or not os.path.exists(path):
        raise HTTPException(404, "사진이 없습니다")
    return FileResponse(path, media_type="image/webp")


@router.post("/damages/save")
async def damage_save(request: Request):
    f = dict(await request.form())
    if not fleet._int(f.get("vehicle_id")):
        return _redirect("/ga/vehicles?tab=damages", "차량을 고르세요")
    fleet.save_damage(f, request.state.user["display_name"])
    nxt = f.get("next") or "/ga/vehicles?tab=damages"
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/vehicles?tab=damages", "사고·파손 기록을 저장했습니다")


@router.post("/damages/{did}/delete")
async def damage_delete(request: Request, did: int):
    f = await request.form()
    repo.execute("DELETE FROM ga_vehicle_damages WHERE id=?", (did,))
    audit("ga", "사고·파손 삭제", f"#{did}", "")
    nxt = f.get("next") or "/ga/vehicles?tab=damages"
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/vehicles?tab=damages", "삭제했습니다")


@router.get("/fines/driver")
def fine_driver(vehicle_id: int, at: str):
    r = fleet.find_driver(vehicle_id, at.replace("T", " "))
    return JSONResponse({"driver_id": r["driver_id"], "message": r["message"]})


@router.post("/fines/save")
async def fine_save(request: Request):
    f = dict(await request.form())
    fid, msg = fleet.save_fine(f)
    nxt = f.get("next") or "/ga/vehicles?tab=fines"
    nxt = nxt if nxt.startswith("/ga/") else "/ga/vehicles?tab=fines"
    if not fid:
        return _redirect(nxt, msg)
    return _redirect(nxt, "과태료를 저장했습니다" + (f" · {msg}" if msg else ""))


@router.post("/fines/{fid}/delete")
async def fine_delete(request: Request, fid: int):
    f = await request.form()
    repo.execute("DELETE FROM ga_vehicle_fines WHERE id=?", (fid,))
    audit("ga", "과태료 삭제", f"#{fid}", "")
    nxt = f.get("next") or "/ga/vehicles?tab=fines"
    return _redirect(nxt if nxt.startswith("/ga/") else "/ga/vehicles?tab=fines", "삭제했습니다")


# ════════════════════════════════════════════════════════════════════════
# 차량 QR (/vq/{열쇠}) — 직원 휴대폰
# ════════════════════════════════════════════════════════════════════════
def _qr(token: str) -> dict:
    v = fleet.vehicle_by_qr(token)
    if not v:
        raise HTTPException(404, "등록되지 않았거나 라벨이 바뀐 차량입니다. 관리부에 문의하세요")
    return v


def _page(request, name, ctx):
    return templates.TemplateResponse(request, name, ctx)


def _person(request: Request, v: dict) -> dict | None:
    return fleet.session_employee(v["id"], request.cookies.get(COOKIE, ""))


def _staff(request: Request) -> bool:
    u = request.state.user
    return bool(u and u["role"] in STAFF)


def _my_insp(v: dict, me: dict, iid: int) -> dict:
    i = fleet.get_inspection(iid)
    if not i or i["vehicle_id"] != v["id"]:
        raise HTTPException(404, "점검을 찾을 수 없습니다")
    if me["id"] not in (i["from_emp_id"], i["to_emp_id"]) and i["created_by"] != f"{me['name']}(emp#{me['id']})":
        raise HTTPException(403, "본인과 관련된 점검만 열 수 있습니다")
    return i


@public_router.get("/{token}", response_class=HTMLResponse)
def qr_home(request: Request, token: str):
    v = _qr(token)
    cur = fleet.current_user(v["id"])
    ctx = {"v": v, "token": token, "cur_masked": _mask(cur["name"]) if cur else "", "error": request.query_params.get("e", ""),
           "msg": request.query_params.get("msg", ""), "staff": _staff(request)}
    if ctx["staff"]:
        ctx["open"] = fleet.open_inspections(v["id"])
        return _page(request, "ga/vehicle_qr.html", ctx)
    me = _person(request, v)
    if me:
        mine = [i for i in fleet.open_inspections(v["id"])
                if me["id"] in (i["from_emp_id"], i["to_emp_id"]) or i["created_by"] == f"{me['name']}(emp#{me['id']})"]
        ctx.update({"me": me, "mine": mine, "self_kinds": fleet.SELF_KINDS})
    return _page(request, "ga/vehicle_qr.html", ctx)


def _mask(name: str | None) -> str:
    if not name:
        return ""
    return name[0] + "*" if len(name) == 2 else name[0] + "*" * (len(name) - 2) + name[-1]


@public_router.post("/{token}/verify")
async def qr_verify(request: Request, token: str):
    v = _qr(token)
    f = await request.form()
    emp, err = fleet.verify_person(v["id"], f.get("name", ""), f.get("birth", ""))
    if not emp:
        return RedirectResponse(f"/vq/{token}?e=" + quote(err), status_code=303)
    s = fleet.open_session(v["id"], emp["id"])
    res = RedirectResponse(f"/vq/{token}", status_code=303)
    res.set_cookie(COOKIE, s, max_age=fleet.SESSION_MINUTES * 60, httponly=True, samesite="strict",
                   secure=cfg.secure_cookie, path="/vq")
    return res


@public_router.post("/{token}/start")
async def qr_start(request: Request, token: str):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return RedirectResponse(f"/vq/{token}", status_code=303)
    kind = (await request.form()).get("kind", "")
    if kind not in fleet.SELF_KINDS + [fleet.REPORT_KIND]:
        return RedirectResponse(f"/vq/{token}?e=" + quote("점검 구분을 고르세요"), status_code=303)
    cur = fleet.current_user(v["id"])
    iid, err = fleet.create_inspection(v["id"], kind, today().isoformat(), cur["employee_id"] if cur else None, None,
                                       "", f"{me['name']}(emp#{me['id']})")
    if err:
        return RedirectResponse(f"/vq/{token}?e=" + quote(err), status_code=303)
    return RedirectResponse(f"/vq/{token}/i/{iid}", status_code=303)


@public_router.get("/{token}/i/{iid}", response_class=HTMLResponse)
def qr_shoot(request: Request, token: str, iid: int):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return RedirectResponse(f"/vq/{token}", status_code=303)
    i = _my_insp(v, me, iid)
    ctx = _shoot_ctx(i, lambda pid, t: f"/vq/{token}/p/{pid}{'?thumb=1' if t else ''}", f"/vq/{token}/i/{iid}",
                     f"/vq/{token}", me)
    ctx["staff"] = False
    ctx["msg"] = request.query_params.get("msg", "")
    return _page(request, "ga/vehicle_shoot.html", ctx)


@public_router.post("/{token}/i/{iid}/photo")
async def qr_photo(request: Request, token: str, iid: int):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return JSONResponse({"ok": False, "message": "본인 확인이 끝났습니다. 다시 확인해 주세요"}, status_code=401)
    _my_insp(v, me, iid)
    return await _upload(request, iid, f"{me['name']}(직원)")


@public_router.post("/{token}/i/{iid}/photo/{pid}/delete")
def qr_photo_delete(request: Request, token: str, iid: int, pid: int):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return JSONResponse({"ok": False, "message": "본인 확인이 끝났습니다"}, status_code=401)
    _my_insp(v, me, iid)
    err = fleet.delete_photo(iid, pid, f"{me['name']}(직원)")
    return JSONResponse({"ok": not err, "message": err}, status_code=400 if err else 200)


@public_router.post("/{token}/i/{iid}/meta")
async def qr_meta(request: Request, token: str, iid: int):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return JSONResponse({"ok": False}, status_code=401)
    _my_insp(v, me, iid)
    f = await request.form()
    fleet.save_meta(iid, f.get("odometer"), f.get("fuel_level", ""), f.get("note"))
    return JSONResponse({"ok": True})


@public_router.post("/{token}/i/{iid}/finish")
def qr_finish(request: Request, token: str, iid: int):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return RedirectResponse(f"/vq/{token}", status_code=303)
    _my_insp(v, me, iid)
    err = fleet.finish_shooting(iid, f"{me['name']}(직원)")
    return RedirectResponse(f"/vq/{token}/i/{iid}?msg=" + quote(err or "촬영을 마쳤습니다. 관리부에 전달되었습니다"), status_code=303)


@public_router.post("/{token}/i/{iid}/confirm")
async def qr_confirm(request: Request, token: str, iid: int):
    v = _qr(token)
    me = _person(request, v)
    if not me:
        return RedirectResponse(f"/vq/{token}", status_code=303)
    i = _my_insp(v, me, iid)
    side = (await request.form()).get("side", "")
    if (side == "from" and me["id"] != i["from_emp_id"]) or (side == "to" and me["id"] != i["to_emp_id"]):
        raise HTTPException(403)
    err = fleet.confirm_party(iid, side, f"{me['name']} (본인 휴대폰 확인)")
    return RedirectResponse(f"/vq/{token}/i/{iid}?msg=" + quote(err or "확인을 기록했습니다"), status_code=303)


@public_router.get("/{token}/p/{pid}")
def qr_photo_file(request: Request, token: str, pid: int, thumb: bool = False):
    v = _qr(token)
    if not (_person(request, v) or _staff(request)):
        raise HTTPException(403)
    p = fleet.photo_belongs(pid, v["id"])
    path = photos.vehicle_path(p["inspection_id"], pid, thumb) if p else ""
    if not p or not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp")


@public_router.post("/{token}/logout")
def qr_logout(token: str):
    res = RedirectResponse(f"/vq/{token}", status_code=303)
    res.delete_cookie(COOKIE, path="/vq")
    return res
