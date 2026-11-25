"""문서 보관함 화면·주소 (/files). 메뉴마다 볼 수 있는 권한이 달라 주소 안에서 다시 확인합니다 (app/archive.py MENUS)."""
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import archive
from app.common import templates
from app.db import audit

router = APIRouter(prefix="/files")


def pending_for(request: Request, menus) -> list[dict]:
    """화면에서 부르는 것: 이 사용자가 볼 수 있는 메뉴의 '폴더에 새로 넣은 파일' 알림. 문제가 생겨도 화면은 뜨게 빈 목록."""
    try:
        role = _role(request)
        return archive.pending([m for m in menus if archive.allowed(m, role)])
    except Exception:                                       # noqa: BLE001
        return []


def _role(request: Request) -> str | None:
    u = getattr(request.state, "user", None)
    return u["role"] if u else None


def _guard(request: Request, menu: str, ref_id: int) -> dict:
    if not archive.allowed(menu, _role(request)):
        raise HTTPException(403, "이 서류를 볼 권한이 없습니다")
    t = archive.target(menu, ref_id)
    if not t:
        raise HTTPException(404, "대상을 찾을 수 없습니다")
    return t


NEXT_OK = re.compile(r"^/(ga|hr|mgmt|files)/[A-Za-z0-9_./-]*(\?tab=[a-z]{1,20})?$")      # 돌아갈 곳: 이 시스템 안의 정해진 화면만


def _next(nxt, default: str = "") -> str:
    nxt = str(nxt or "")
    return nxt if NEXT_OK.match(nxt) and "//" not in nxt and ".." not in nxt else default


def _with_msg(url: str, msg: str) -> str:
    return url + ("&" if "?" in url else "?") + "msg=" + quote(msg)


def _back(menu: str, ref_id: int, msg: str, nxt: str = "") -> RedirectResponse:
    nxt = _next(nxt)
    if nxt:
        return RedirectResponse(_with_msg(nxt, msg) + "#files", status_code=303)
    return RedirectResponse(f"/files/{menu}/{ref_id}?msg=" + quote(msg), status_code=303)


def panel(menu: str, ref_id: int, nxt: str = "") -> dict | None:
    """상세 화면 안에 넣는 '서류' 묶음(올리기·대장에 없는 파일·붙인 서류)에 필요한 값."""
    t = archive.target(menu, ref_id)
    if not t:
        return None
    archive.ensure_quiet(menu, ref_id)                      # 폴더를 먼저 준비 (손으로 만든 비슷한 폴더가 있으면 그 폴더를 씀)
    return {"menu": menu, "ref_id": ref_id, "rows": archive.items(menu, ref_id), "info": archive.MENUS[menu],
            "waiting": archive.unregistered(menu, ref_id), "next": nxt,
            "folder": " \\ ".join(archive.folder(menu, t).replace("/", "\\").split("\\")), "max_mb": archive.MAX_MB}


@router.get("/get/{att_id}")
def download(request: Request, att_id: int, dl: int = 0):
    att = archive.get(att_id)
    if not att or not archive.allowed(att["menu"], _role(request)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    try:
        data = archive.read(att)
    except archive.ArchiveError as ex:
        raise HTTPException(409, str(ex)) from ex
    audit("files", "서류 열람", att["orig_name"], f"{att['menu']} #{att['ref_id']}")
    inline = att["mime"] in archive.INLINE and not dl
    return Response(data, media_type=att["mime"] if inline else "application/octet-stream", headers={
        "Content-Disposition": ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(att["orig_name"]),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/del/{att_id}")
async def remove(request: Request, att_id: int):
    att = archive.get(att_id)
    if not att or not archive.allowed(att["menu"], _role(request)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    nxt = str((await request.form()).get("next") or "")
    archive.delete(att_id)
    archive.pending_clear(att["menu"])
    return _back(att["menu"], att["ref_id"], "지웠습니다 (파일은 보관함의 '_삭제함' 폴더로 옮겼습니다)", nxt)


@router.post("/adopt-all")
async def adopt_all(request: Request):
    """알림에 보인 대상들의 '대장에 없는 파일'을 한꺼번에 대장에 올림. items = 'menu:번호' 여러 개."""
    form = await request.form()
    back = _next(form.get("next"), "/ga/contracts")
    total, errs = 0, []
    for it in [str(x) for x in form.getlist("items")][:100]:
        menu, _, rid = it.partition(":")
        if not rid.isdigit() or not archive.allowed(menu, _role(request)) or menu not in archive.PENDING or archive.PENDING[menu][2]:
            continue                                        # 한 폴더를 여러 건이 같이 쓰는 메뉴(과태료 등)는 어느 건인지 사람이 골라야 함
        names = [w["name"] for w in archive.unregistered(menu, int(rid))]
        if not names:
            continue
        try:
            ok, e = await run_in_threadpool(archive.adopt, menu, int(rid), names, "")
        except archive.ArchiveError as ex:
            errs.append(str(ex))
            continue
        total += ok
        errs += e
    msg = (f"{total}개를 대장에 올렸습니다 (파일은 폴더에 그대로 있습니다)" if total else "올릴 파일이 없습니다") \
        + ((" · 올리지 못한 파일 — " + " / ".join(errs[:5])) if errs else "")
    return RedirectResponse(_with_msg(back, msg), status_code=303)


@router.get("/{menu}/{ref_id}", response_class=HTMLResponse)
def page(request: Request, menu: str, ref_id: int):
    t = _guard(request, menu, ref_id)
    fp = panel(menu, ref_id)
    return templates.TemplateResponse(request, "files.html", {
        "menu": menu, "ref_id": ref_id, "t": t, "info": archive.MENUS[menu], "fp": fp,
        "active_menu": {"ga.fines": "ga.vehicles", "ga.damages": "ga.vehicles"}.get(menu, menu),
        "msg": request.query_params.get("msg", ""),
    })


@router.post("/{menu}/{ref_id}/adopt")
async def adopt(request: Request, menu: str, ref_id: int):
    """폴더에 미리 넣어 둔 파일을 복사 없이 대장에 올림."""
    _guard(request, menu, ref_id)
    form = await request.form()
    names = [str(x) for x in form.getlist("names")][:200]
    nxt = str(form.get("next") or "")
    if not names:
        return _back(menu, ref_id, "대장에 올릴 파일을 고르세요", nxt)
    try:
        ok, errs = await run_in_threadpool(archive.adopt, menu, ref_id, names, str(form.get("note") or ""))
    except archive.ArchiveError as ex:
        return _back(menu, ref_id, str(ex), nxt)
    msg = (f"{ok}개를 대장에 올렸습니다 (파일은 폴더에 그대로 있습니다)" if ok else "") + ((" · 올리지 못한 파일 — " + " / ".join(errs)) if errs else "")
    return _back(menu, ref_id, msg.strip(" ·") or "올릴 파일이 없습니다", nxt)


@router.post("/{menu}/{ref_id}/upload")
async def upload(request: Request, menu: str, ref_id: int):
    _guard(request, menu, ref_id)
    form = await request.form()
    note = str(form.get("note") or "")
    nxt = str(form.get("next") or "")
    ok, errs = 0, []
    for up in form.getlist("files")[:20]:
        name = getattr(up, "filename", "") or ""
        if not name:
            continue
        raw = await up.read(archive.MAX_MB * 1024 * 1024 + 1)
        try:
            await run_in_threadpool(archive.save, menu, ref_id, name, raw, note)
            ok += 1
        except archive.ArchiveError as ex:
            errs.append(f"{name}: {ex}")
    if not ok and not errs:
        return _back(menu, ref_id, "올릴 파일을 고르세요", nxt)
    msg = (f"{ok}개를 보관함에 저장했습니다" if ok else "") + ((" · 저장하지 못한 파일 — " + " / ".join(errs)) if errs else "")
    archive.pending_clear(menu)
    return _back(menu, ref_id, msg.strip(" ·"), nxt)
