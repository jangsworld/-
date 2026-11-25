"""문서 보관함 화면·주소 (/files). 메뉴마다 볼 수 있는 권한이 달라 주소 안에서 다시 확인합니다 (app/archive.py MENUS)."""
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import archive, archive_ops
from app.common import templates
from app.db import audit

router = APIRouter(prefix="/files")


class Rows(list):
    recent: list = []            # 최근에 NAS 에서 자동으로 들어온 서류
    hold: str = ""               # 대량 변경이 의심되어 자동 반영을 멈춘 때


def pending_for(request: Request, menus) -> list[dict]:
    """화면에서 부르는 것: 이 사용자가 볼 수 있는 메뉴의 '폴더에 새로 넣은 파일' 알림. 문제가 생겨도 화면은 뜨게 빈 목록."""
    try:
        role = _role(request)
        ok = [m for m in menus if archive.allowed(m, role)]
        # 자동으로 올라가는 건은 '올리세요' 알림에서 뺌 (아래 '최근 들어온 서류'로 알려 줌)
        rows = Rows(p for p in archive.pending(ok) if p["shared"] or not archive_ops.auto_for(p["menu"], p["ref_id"]))
        rows.recent = archive_ops.recent(ok)
        rows.hold = archive_ops.held() if ok else ""
        return rows
    except Exception:                                       # noqa: BLE001
        return Rows()


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


FILE_LOOK = {      # 확장자 → (아이콘, 색 이름, 종류 이름)  — 탐색기처럼 보이게
    "pdf": ("bi-file-earmark-pdf-fill", "pdf", "PDF 문서"), "jpg": ("bi-file-earmark-image-fill", "img", "사진"),
    "jpeg": ("bi-file-earmark-image-fill", "img", "사진"), "png": ("bi-file-earmark-image-fill", "img", "사진"),
    "webp": ("bi-file-earmark-image-fill", "img", "사진"), "hwp": ("bi-file-earmark-text-fill", "hwp", "한글 문서"),
    "hwpx": ("bi-file-earmark-text-fill", "hwp", "한글 문서"), "doc": ("bi-file-earmark-word-fill", "doc", "워드 문서"),
    "docx": ("bi-file-earmark-word-fill", "doc", "워드 문서"), "xls": ("bi-file-earmark-excel-fill", "xls", "엑셀 문서"),
    "xlsx": ("bi-file-earmark-excel-fill", "xls", "엑셀 문서"), "pptx": ("bi-file-earmark-ppt-fill", "ppt", "파워포인트"),
}


def _explorer(menu: str, ref_id: int, rows: list[dict], waiting: list[dict]) -> dict:
    """탐색기 화면용: 폴더 목록과 파일 목록(대장에 올린 것 + 폴더에만 있는 새 파일)을 한데 모음."""
    def look(name: str):
        return FILE_LOOK.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", ("bi-file-earmark-fill", "etc", "파일"))

    entries = []
    lost = archive_ops.lost_ids(menu, ref_id, rows)
    auto, bad = archive_ops.auto_for(menu, ref_id), archive_ops.refused(menu, ref_id, waiting)
    thumbs = not menu.startswith("hr.")                     # 인사 서류(신분증 사본 등)는 열람 기록이 남는 '열기'로만 봄
    for r in rows:
        icon, tone, kind = look(r["orig_name"])
        entries.append({"dir": r.get("subdir") or "", "name": r["orig_name"], "icon": icon, "tone": tone, "kind": kind, "size": r["size"],
                        "date": (r.get("uploaded_at") or "")[:16], "by": r.get("uploaded_by") or "", "note": r.get("note") or "",
                        "id": r["id"], "inline": r["inline"], "new": False, "lost": r["id"] in lost, "edited": (r.get("modified_at") or "")[5:16],
                        "changed": r["id"] not in lost and bool(r.get("mtime")) and archive_ops.changed_path(r) is not None,
                        "view": ("pdf" if r["mime"] == "application/pdf" else "img") if r["inline"] else "",
                        "thumb": thumbs and r["mime"].startswith("image/") and r["id"] not in lost})
    for w in waiting:
        icon, tone, kind = look(w["file"])
        entries.append({"dir": w["sub"], "name": w["file"], "icon": icon, "tone": tone, "kind": kind, "size": w["size"], "date": "", "by": "",
                        "note": "", "id": None, "inline": False, "new": True, "path": w["name"], "bad": w["name"] in bad})
    entries.sort(key=lambda e: (e["dir"], e["name"].lower()))
    paths = set()
    try:
        paths.update(archive.subdirs(menu, ref_id))
    except Exception:                                       # noqa: BLE001
        pass
    for e in entries:                                       # 파일이 들어 있는 폴더와 그 위 폴더들
        parts = e["dir"].split("/") if e["dir"] else []
        for i in range(1, len(parts) + 1):
            paths.add("/".join(parts[:i]))
    folders = []
    for p in sorted(paths):
        inside = [e for e in entries if e["dir"] == p or e["dir"].startswith(p + "/")]
        folders.append({"path": p, "name": p.rsplit("/", 1)[-1], "parent": p.rsplit("/", 1)[0] if "/" in p else "",
                        "count": len(inside), "new": sum(1 for e in inside if e["new"])})
    # 잠시 뒤 다시 그리면 바뀔 것(자동 등록·고친 내용 반영을 기다리는 것)의 수: 화면이 스스로 새로 고침
    wait = sum(1 for e in entries if (e["new"] and auto and not e["bad"]) or (e.get("changed") and archive_ops.auto_on() and not archive_ops.held()))
    return {"entries": entries, "folders": folders, "auto": auto, "wait": wait}


def panel(menu: str, ref_id: int, nxt: str = "") -> dict | None:
    """상세 화면 안에 넣는 '서류' 묶음(올리기·대장에 없는 파일·붙인 서류)에 필요한 값."""
    t = archive.target(menu, ref_id)
    if not t:
        return None
    archive.ensure_quiet(menu, ref_id)                      # 폴더를 먼저 준비 (손으로 만든 비슷한 폴더가 있으면 그 폴더를 씀)
    archive_ops.sync_quiet(menu, ref_id)                    # NAS 에 직접 넣은 파일을 대장에 (복사가 끝난 것만)
    rows, waiting = archive.items(menu, ref_id), archive.unregistered(menu, ref_id)
    return {"menu": menu, "ref_id": ref_id, "rows": rows, "info": archive.MENUS[menu],
            "waiting": waiting, "loose": archive.loose(menu, ref_id), "next": nxt, **_explorer(menu, ref_id, rows, waiting),
            "folder": " \\ ".join(archive.folder(menu, t).replace("/", "\\").split("\\")), "max_mb": archive.MAX_MB}


@router.get("/get/{att_id}")
def download(request: Request, att_id: int, dl: int = 0):
    att = archive.get(att_id)
    if not att or not archive.allowed(att["menu"], _role(request)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    try:
        data = archive_ops.read_fresh(att)
    except (archive.ArchiveError, OSError) as ex:
        raise HTTPException(409, str(ex) if isinstance(ex, archive.ArchiveError) else "파일을 읽을 수 없습니다") from ex
    audit("files", "서류 열람", att["orig_name"], f"{att['menu']} #{att['ref_id']}")
    inline = att["mime"] in archive.INLINE and not dl
    return Response(data, media_type=att["mime"] if inline else "application/octet-stream", headers={
        "Content-Disposition": ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(att["orig_name"]),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/thumb/{att_id}")
def thumbnail(request: Request, att_id: int):
    """사진을 작은 그림으로 (총무 메뉴만)."""
    att = archive.get(att_id)
    if not att or not archive.allowed(att["menu"], _role(request)) or att["menu"].startswith("hr.") or not att["mime"].startswith("image/"):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    try:
        data = archive_ops.thumb(att)
    except Exception as ex:                                 # noqa: BLE001  (깨진 사진이면 아이콘으로 보임)
        raise HTTPException(404, "미리 볼 수 없는 사진입니다") from ex
    return Response(data, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/del/{att_id}")
async def remove(request: Request, att_id: int):
    att = archive.get(att_id)
    if not att or not archive.allowed(att["menu"], _role(request)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    nxt = str((await request.form()).get("next") or "")
    try:
        await run_in_threadpool(archive.delete, att_id)
    except archive.ArchiveError as ex:
        return _back(att["menu"], att["ref_id"], str(ex), nxt)
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


@router.get("/{menu}/{ref_id}/panel", response_class=HTMLResponse)
def panel_fragment(request: Request, menu: str, ref_id: int, next: str = ""):      # noqa: A002
    """탐색기 부분만 다시 그림 (새로 고침 단추, 올리기·옮기기 뒤)."""
    _guard(request, menu, ref_id)
    return templates.TemplateResponse(request, "_fx.html", {"fp": panel(menu, ref_id, _next(next))}, headers={"Cache-Control": "no-store"})


@router.post("/{menu}/{ref_id}/op")
async def op(request: Request, menu: str, ref_id: int):
    """탐색기 동작: 새 폴더(mkdir) · 옮기기(move) · 이름 바꾸기(rename, rename_dir) · 지우기(trash, trash_dir)."""
    _guard(request, menu, ref_id)
    try:
        b = await request.json()
        kind = str(b.get("op") or "")
        ids = [int(x) for x in (b.get("ids") or [])][:200]
        calls = {
            "mkdir": lambda: archive_ops.mkdir(menu, ref_id, b.get("dir"), b.get("name")),
            "move": lambda: archive_ops.move(menu, ref_id, ids, b.get("dir")),
            "rename": lambda: archive_ops.rename_file(menu, ref_id, ids[0] if ids else 0, b.get("name")),
            "rename_dir": lambda: archive_ops.rename_dir(menu, ref_id, b.get("dir"), b.get("name")),
            "trash": lambda: archive_ops.trash_files(menu, ref_id, ids),
            "accept": lambda: archive_ops.accept_edit(menu, ref_id, ids[0] if ids else 0),
            "trash_dir": lambda: archive_ops.trash_dir(menu, ref_id, b.get("dir")),
        }
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False, "msg": "잘못된 요청입니다"}, status_code=400)
    if kind not in calls:
        return JSONResponse({"ok": False, "msg": "알 수 없는 동작입니다"}, status_code=400)
    try:
        return {"ok": True, "msg": await run_in_threadpool(calls[kind])}
    except archive.ArchiveError as ex:
        return {"ok": False, "msg": str(ex)}


@router.post("/{menu}/{ref_id}/claim")
async def claim(request: Request, menu: str, ref_id: int):
    """위 폴더('과태료' 등)에 바로 넣어 둔 파일을 이 건으로 가져옴."""
    _guard(request, menu, ref_id)
    form = await request.form()
    nxt = str(form.get("next") or "")
    try:
        msg = await run_in_threadpool(archive_ops.claim, menu, ref_id, [str(x) for x in form.getlist("names")][:100])
    except archive.ArchiveError as ex:
        msg = str(ex)
    return _back(menu, ref_id, msg, nxt)


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
    ajax = request.headers.get("x-fx") == "1"               # 탐색기에서 끌어다 놓기·올리기
    ok, errs = 0, []
    try:
        where = archive_ops.clean_dir(menu, form.get("dir"))
    except archive.ArchiveError as ex:
        return {"ok": False, "msg": str(ex)} if ajax else _back(menu, ref_id, str(ex), nxt)
    for up in form.getlist("files")[:20]:
        name = getattr(up, "filename", "") or ""
        if not name:
            continue
        raw = await up.read(archive.MAX_MB * 1024 * 1024 + 1)
        try:
            await run_in_threadpool(archive.save, menu, ref_id, name, raw, note, where)
            ok += 1
        except archive.ArchiveError as ex:
            errs.append(f"{name}: {ex}")
    if not ok and not errs:
        return {"ok": False, "msg": "올릴 파일을 고르세요"} if ajax else _back(menu, ref_id, "올릴 파일을 고르세요", nxt)
    msg = (f"{ok}개를 보관함에 저장했습니다" if ok else "") + ((" · 저장하지 못한 파일 — " + " / ".join(errs)) if errs else "")
    archive.pending_clear(menu)
    if ajax:
        return {"ok": ok > 0 and not errs, "msg": msg.strip(" ·")}
    return _back(menu, ref_id, msg.strip(" ·"), nxt)
