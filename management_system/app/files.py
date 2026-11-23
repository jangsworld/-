"""문서 보관함 화면·주소 (/files). 메뉴마다 볼 수 있는 권한이 달라 주소 안에서 다시 확인합니다 (app/archive.py MENUS)."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import archive
from app.common import templates
from app.db import audit

router = APIRouter(prefix="/files")


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


def _back(menu: str, ref_id: int, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/files/{menu}/{ref_id}?msg=" + quote(msg), status_code=303)


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
def remove(request: Request, att_id: int):
    att = archive.get(att_id)
    if not att or not archive.allowed(att["menu"], _role(request)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    archive.delete(att_id)
    return _back(att["menu"], att["ref_id"], "지웠습니다 (파일은 보관함의 '_삭제함' 폴더로 옮겼습니다)")


@router.get("/{menu}/{ref_id}", response_class=HTMLResponse)
def page(request: Request, menu: str, ref_id: int):
    t = _guard(request, menu, ref_id)
    return templates.TemplateResponse(request, "files.html", {
        "menu": menu, "ref_id": ref_id, "t": t, "rows": archive.items(menu, ref_id), "info": archive.MENUS[menu],
        "folder": " \\ ".join(archive.folder(menu, t).replace("/", "\\").split("\\")), "max_mb": archive.MAX_MB,
        "active_menu": {"ga.fines": "ga.vehicles", "ga.damages": "ga.vehicles"}.get(menu, menu),
        "msg": request.query_params.get("msg", ""),
    })


@router.post("/{menu}/{ref_id}/upload")
async def upload(request: Request, menu: str, ref_id: int):
    _guard(request, menu, ref_id)
    form = await request.form()
    note = str(form.get("note") or "")
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
        return _back(menu, ref_id, "올릴 파일을 고르세요")
    msg = (f"{ok}개를 보관함에 저장했습니다" if ok else "") + ((" · 저장하지 못한 파일 — " + " / ".join(errs)) if errs else "")
    return _back(menu, ref_id, msg.strip(" ·"))
