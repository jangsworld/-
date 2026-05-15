"""Document folder scan and file upload/serve API."""
import os
import mimetypes
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse

router = APIRouter(tags=["documents"])


def _get_docs_root(request: Request) -> Path:
    return request.app.state.documents_root


def _safe_path(docs_root: Path, rel_path: str) -> Path:
    """Resolve rel_path relative to docs_root; raise 400 if outside."""
    target = (docs_root / rel_path).resolve()
    if not str(target).startswith(str(docs_root.resolve())):
        raise HTTPException(status_code=400, detail="잘못된 경로입니다")
    return target


@router.get("/documents/folders")
def list_folders(request: Request):
    docs_root = _get_docs_root(request)
    folders = []
    for item in sorted(docs_root.iterdir()):
        if item.is_dir():
            folders.append({"name": item.name, "path": item.name})
    return folders


@router.get("/documents/browse")
def browse_folder(request: Request, path: str = Query(...)):
    docs_root = _get_docs_root(request)
    target = _safe_path(docs_root, path)
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="디렉토리가 아닙니다")
    files = []
    for item in sorted(target.iterdir()):
        if item.is_file():
            stat = item.stat()
            files.append({
                "name": item.name,
                "path": str(item.relative_to(docs_root)).replace("\\", "/"),
                "size": stat.st_size,
                "extension": item.suffix.lower().lstrip("."),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return files


@router.post("/documents/upload")
async def upload_file(
    request: Request,
    folder: str = Query(...),
    file: UploadFile = File(...),
):
    docs_root = _get_docs_root(request)
    target_dir = _safe_path(docs_root, folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="파일명이 올바르지 않습니다")

    dest = target_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)

    rel_path = str(dest.relative_to(docs_root)).replace("\\", "/")
    return {"path": rel_path, "message": "업로드 완료"}


@router.get("/documents/file")
def serve_file(request: Request, path: str = Query(...)):
    docs_root = _get_docs_root(request)
    target = _safe_path(docs_root, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다")

    mime, _ = mimetypes.guess_type(str(target))
    if mime is None:
        mime = "application/octet-stream"

    return FileResponse(
        path=str(target),
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{target.name}"'},
    )
