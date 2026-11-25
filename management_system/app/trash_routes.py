"""휴지통 화면."""
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import trash
from app.common import templates

router = APIRouter(prefix="/trash")


def _back(form, msg: str) -> RedirectResponse:
    t = str(form.get("kind") or "")
    url = "/trash?" + (f"kind={quote(t)}&" if t in trash.META else "") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


@router.get("", response_class=HTMLResponse)
def page(request: Request, kind: str = "", q: str = "", soon: int = 0):
    role = request.state.user["role"]
    kind = kind if trash.allowed(kind, role) else ""
    rows = trash.items(role, kind, q.strip())
    if soon:
        rows = [r for r in rows if r["left"] is not None and r["left"] <= trash.WARN_DAYS]
    return templates.TemplateResponse(request, "trash.html", {
        "rows": rows, "kinds": trash.kinds(role), "kind": kind, "q": q.strip(), "soon": soon, "days": trash.DAYS,
        "active_menu": "trash", "msg": request.query_params.get("msg", ""),
    })


@router.get("/{tid:int}/view")
def view(request: Request, tid: int):
    try:
        return {"ok": True, "rows": trash.detail(tid, request.state.user["role"])}
    except trash.TrashError as ex:
        return {"ok": False, "message": str(ex)}


@router.post("/{tid:int}/restore")
async def restore(request: Request, tid: int):
    f = await request.form()
    try:
        msg, url = await run_in_threadpool(trash.restore, tid, request.state.user["role"])
    except trash.TrashError as ex:
        return _back(f, str(ex))
    except sqlite3.IntegrityError:
        return _back(f, "이어져 있던 자료가 달라져 되살릴 수 없습니다")
    if f.get("edit") and url:
        base, _, frag = url.partition("#")
        return RedirectResponse(base + ("&" if "?" in base else "?") + "msg=" + quote(msg) + ("#" + frag if frag else ""), status_code=303)
    return _back(f, msg)


@router.post("/{tid:int}/purge")
async def purge(request: Request, tid: int):
    f = await request.form()
    try:
        title = await run_in_threadpool(trash.purge, tid, request.state.user["role"])
    except trash.TrashError as ex:
        return _back(f, str(ex))
    return _back(f, f"'{title}'을(를) 완전히 지웠습니다")
