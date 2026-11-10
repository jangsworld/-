"""생성 문서(PDF)의 NAS 보관.

employee_root 가 설정되어 있으면 해당 임직원 서류 폴더에 저장합니다.
  {employee_root}/홍길동/BMS_근로계약서_정규직_홍길동_20260301_120000_d12.pdf
미설정 시 nas_root 아래 구조로 저장합니다 (다른 엔진과 겹치지 않도록 'BMS_' 접두어 폴더 사용)
  {nas_root}/BMS_인사/근로계약서/2026/2026001_홍길동_정규직_20260301_d12.pdf
  {nas_root}/BMS_인사/증명서/2026/...
nas_root 도 미설정이면 data/outbox 아래에 같은 구조로 저장합니다(개발용).
PDF 변환은 서버에 설치된 Edge/Chromium(Playwright)으로 수행합니다.
"""
import hashlib
import os
import re
from datetime import datetime

from app.config import APP_DIR, settings

GROUP_FOLDER = {"contract": "근로계약서", "certificate": "증명서"}


class StorageError(Exception):
    pass


def _safe(name: str) -> str:
    """Windows 파일명 금지문자·경로 구분자 제거."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    return name[:60] or "noname"


def _inside(root: str, path: str) -> bool:
    """경로 이탈 방지. 드라이브가 다른 경로는 commonpath 가 ValueError 를 냅니다."""
    try:
        return bool(root) and os.path.commonpath([os.path.normpath(root), path]) == os.path.normpath(root)
    except ValueError:
        return False


def employee_path(doc: dict) -> str:
    folder = os.path.join(settings.employee_root, _safe(doc.get("folder_name") or doc["name"]))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    group = GROUP_FOLDER.get(doc["doc_group"], "기타")
    fname = f"BMS_{group}_{_safe(doc['category'])}_{_safe(doc['name'])}_{stamp}_d{int(doc['id'])}.pdf"
    path = os.path.normpath(os.path.join(folder, fname))
    if not _inside(settings.employee_root, path):
        raise StorageError("저장 경로가 올바르지 않습니다")
    return path


def target_path(doc: dict) -> str:
    if settings.employee_root:
        return employee_path(doc)
    year = (doc.get("start_date") or doc.get("created_at") or datetime.now().isoformat())[:4]
    folder = os.path.join(settings.output_root, "BMS_인사", GROUP_FOLDER.get(doc["doc_group"], "기타"), _safe(year))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{_safe(doc['emp_no'])}_{_safe(doc['name'])}_{_safe(doc['category'])}_{stamp}_d{int(doc['id'])}.pdf"
    path = os.path.normpath(os.path.join(folder, fname))
    if not _inside(settings.output_root, path):
        raise StorageError("저장 경로가 올바르지 않습니다")
    return path


def _paper_css() -> str:
    with open(os.path.join(APP_DIR, "static", "css", "app.css"), encoding="utf-8") as f:
        css = f.read()
    from app.common import doc_font_css
    return css + "\nbody{background:#fff;margin:0}.paper{box-shadow:none;margin:0;padding:0;width:auto;min-height:auto}" \
                 ".ph-missing{background:none;color:#111}.sign-table{page-break-inside:avoid}" + doc_font_css()


def render_pdf(content_html: str, title: str) -> bytes:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as ex:
        raise StorageError("PDF 변환 모듈(playwright)이 설치되어 있지 않습니다: pip install playwright") from ex
    html = (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>{_safe(title)}</title>"
            f"<style>{_paper_css()}</style></head><body><div class='paper'>{content_html}</div></body></html>")
    opts = {}
    if settings.pdf_browser_path:
        opts["executable_path"] = settings.pdf_browser_path
    elif settings.pdf_browser_channel:
        opts["channel"] = settings.pdf_browser_channel
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**opts)
            try:
                page = browser.new_page()
                # 외부 네트워크 요청 차단(문서는 자체 HTML·CSS만 사용)
                page.route("**/*", lambda r: r.abort() if not r.request.url.startswith(("data:", "about:")) else r.continue_())
                page.set_content(html, wait_until="load")
                return page.pdf(format="A4", print_background=True, margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"})
            finally:
                browser.close()
    except StorageError:
        raise
    except Exception as ex:
        raise StorageError(f"PDF 변환 실패(브라우저 확인 필요): {ex}") from ex


def save_document_pdf(doc: dict) -> tuple[str, str]:
    """문서를 PDF로 변환해 NAS에 저장. (경로, sha256) 반환."""
    pdf = render_pdf(doc["content"], doc["title"])
    path = target_path(doc)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(pdf)
        os.replace(tmp, path)
    except OSError as ex:
        raise StorageError(f"PDF 저장 실패: {ex.__class__.__name__} (NAS 연결·공유폴더 권한을 확인하세요)") from ex
    return path, hashlib.sha256(pdf).hexdigest()


def read_verified(path: str, sha256: str) -> bytes:
    path = os.path.normpath(path)
    if not (_inside(settings.employee_root, path) or _inside(settings.output_root, path)):
        raise StorageError("허용되지 않은 경로")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as ex:
        raise StorageError(f"파일을 읽을 수 없습니다: {ex}") from ex
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise StorageError("파일 해시가 일치하지 않습니다 (위·변조 또는 손상 의심)")
    return data
