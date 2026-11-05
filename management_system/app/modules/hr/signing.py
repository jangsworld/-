"""근로계약서 전자서명: 메일 링크 → 본인 확인 → 서명 → 체결완료 → PDF 보관 → 사본 교부.

직원은 시스템 계정이 없으므로 /sign/ 경로만 로그인 없이 열립니다.
  - 링크 토큰: 256비트 난수, DB 에는 해시로 조회 (담당자 재확인용 원문은 암호화 저장)
  - 본인 확인: 생년월일 6자리, 5회 실패 시 링크 잠금
  - 위·변조 방지: 발송 시점 본문 해시와 서명 시점 본문이 다르면 서명 불가
  - 증빙: 서명 일시·IP·브라우저·문서 해시를 문서와 DB 에 기록
"""
import base64
import hashlib
import html
import logging
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import mailer
from app.common import templates, today
from app.config import settings
from app.db import audit, execute, get_settings, query_one
from app.modules.hr import repo, services
from app.security.crypto import enc

log = logging.getLogger("bms")
router = APIRouter(prefix="/sign")

COOKIE = "bms_sign"
MAX_FAILS = 5
SESSION_MINUTES = 30
MAX_SIGNATURE_BYTES = 300 * 1024


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def content_hash(content: str) -> str:
    return _hash(content or "")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def base_url(request: Request) -> str:
    return (get_settings().get("public_url") or settings.public_url or str(request.base_url)).rstrip("/")


def latest_request(doc_id: int) -> dict | None:
    return query_one("SELECT * FROM hr_sign_requests WHERE doc_id=? ORDER BY id DESC LIMIT 1", (doc_id,))


def is_signed(doc_id: int) -> bool:
    return bool(query_one("SELECT 1 x FROM hr_sign_requests WHERE doc_id=? AND status='완료'", (doc_id,)))


def create_request(doc: dict, emp: dict, base: str) -> tuple[str, bool, str]:
    """서명 요청 생성·발송. (링크, 메일 발송 여부, 메일 오류) 반환."""
    token = secrets.token_urlsafe(32)
    link = f"{base}/sign/{token}"
    execute("UPDATE hr_sign_requests SET status='취소' WHERE doc_id=? AND status='대기'", (doc["id"],))
    expires = (datetime.now() + timedelta(days=settings.sign_valid_days)).strftime("%Y-%m-%d %H:%M:%S")
    req_id = execute(
        "INSERT INTO hr_sign_requests(doc_id, token_hash, token, email, content_sha256, expires_at, sent_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc["id"], _hash(token), enc(token), enc(emp["email"]), content_hash(doc["content"]), expires, _now()),
    )
    repo.update_document(doc["id"], status="서명대기")
    company = get_settings().get("company_name", "")
    mailed, error = False, ""
    if mailer.configured():
        body = (f"{emp['name']}님, 안녕하세요.\n\n{company}에서 '{doc['title']}' 전자서명을 요청드립니다.\n"
                f"아래 링크에서 생년월일 6자리로 본인 확인 후 내용을 확인하고 서명해 주세요.\n\n{link}\n\n"
                f"링크 유효기간: {expires[:10]}까지\n본인이 요청받은 내용이 아니면 경영관리팀으로 알려주세요.")
        try:
            mailer.send(emp["email"], f"[{company}] 전자서명 요청", body)
            mailed = True
            execute("UPDATE hr_sign_requests SET mailed=1 WHERE id=?", (req_id,))
        except mailer.MailError as ex:
            error = str(ex)
    audit("hr", "전자서명 요청", f"doc#{doc['id']}", "메일 발송" if mailed else "링크 생성(메일 미발송)")
    return link, mailed, error


# ── 직원 화면 ──────────────────────────────────────────────────────────
def _page(request: Request, name: str, ctx: dict):
    return templates.TemplateResponse(request, name, ctx)


def _message(request: Request, title: str, text: str):
    return _page(request, "sign/message.html", {"title": title, "text": text})


def _load(token: str) -> tuple[dict | None, str]:
    """(요청, 사용 불가 사유)."""
    req = query_one("SELECT * FROM hr_sign_requests WHERE token_hash=?", (_hash(token),))
    if not req or req["status"] == "취소":
        return None, "유효하지 않은 링크입니다. 담당자에게 다시 요청해 주세요."
    if req["status"] == "완료":
        return req, "이미 서명이 완료된 문서입니다. 메일로 받은 사본을 확인해 주세요."
    if req["expires_at"] < _now():
        return None, "링크 유효기간이 지났습니다. 담당자에게 다시 요청해 주세요."
    if req["fail_count"] >= MAX_FAILS:
        return None, "본인 확인을 여러 번 실패해 링크가 잠겼습니다. 담당자에게 문의해 주세요."
    return req, ""


def _verified(request: Request, req: dict) -> bool:
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie and req["session_hash"] == _hash(cookie) and (req["session_expires"] or "") > _now())


@router.get("/{token}", response_class=HTMLResponse)
def sign_page(request: Request, token: str):
    req, problem = _load(token)
    if problem:
        return _message(request, "전자서명", problem)
    if not _verified(request, req):
        return _page(request, "sign/verify.html", {"token": token, "error": request.query_params.get("e", "")})
    doc = repo.get_document(req["doc_id"])
    if content_hash(doc["content"]) != req["content_sha256"]:
        return _message(request, "전자서명", "문서가 변경되어 서명할 수 없습니다. 담당자에게 다시 요청해 주세요.")
    execute("UPDATE hr_sign_requests SET opened_at=COALESCE(opened_at, ?) WHERE id=?", (_now(), req["id"]))
    return _page(request, "sign/document.html", {"token": token, "doc": doc})


@router.post("/{token}/verify")
async def sign_verify(request: Request, token: str):
    req, problem = _load(token)
    if problem:
        return _message(request, "전자서명", problem)
    form = await request.form()
    doc = repo.get_document(req["doc_id"])
    emp = repo.get_employee(doc["employee_id"])
    expected = (emp.get("birth_date") or "").replace("-", "")[2:]
    given = re.sub(r"\D", "", str(form.get("birth", "")))
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        execute("UPDATE hr_sign_requests SET fail_count=fail_count+1 WHERE id=?", (req["id"],))
        audit("hr", "전자서명 본인확인 실패", f"doc#{doc['id']}", "")
        return RedirectResponse(f"/sign/{token}?e=" + quote("생년월일이 일치하지 않습니다"), status_code=303)
    session = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(minutes=SESSION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE hr_sign_requests SET session_hash=?, session_expires=?, fail_count=0 WHERE id=?",
            (_hash(session), expires, req["id"]))
    res = RedirectResponse(f"/sign/{token}", status_code=303)
    res.set_cookie(COOKIE, session, max_age=SESSION_MINUTES * 60, httponly=True, samesite="strict",
                   secure=settings.secure_cookie, path="/sign")
    return res


def _signature_png(value: str) -> str | None:
    """캔버스 서명 이미지 검증: PNG 데이터 URL 만 허용."""
    prefix = "data:image/png;base64,"
    if not value.startswith(prefix):
        return None
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except ValueError:
        return None
    if len(raw) > MAX_SIGNATURE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return prefix + base64.b64encode(raw).decode()


def _signature_block(doc: dict, png: str, signed_at: str, ip: str, sha: str) -> str:
    e = html.escape
    return (
        '<div class="esign-block"><h3>전자서명 확인</h3><table class="doc-table">'
        f'<tr><th>근로자</th><td>{e(doc["name"])}</td><th>서명</th><td><img src="{png}" alt="서명" class="esign-img"></td></tr>'
        f'<tr><th>서명 일시</th><td>{e(signed_at)}</td><th>접속 IP</th><td>{e(ip)}</td></tr>'
        f'<tr><th>문서 해시</th><td colspan="3" class="esign-hash">{e(sha)}</td></tr>'
        '</table><p class="doc-note">본 문서는 위 근로자가 본인 확인 후 전자적으로 서명하였으며, 근로자에게 전자문서로 교부되었습니다.</p></div>'
    )


def _finish(req: dict, png: str, ip: str, agent: str) -> tuple[bool, str]:
    doc = repo.get_document(req["doc_id"])
    signed_at = _now()
    content = doc["content"] + _signature_block(doc, png, signed_at, ip, req["content_sha256"])
    repo.update_document(doc["id"], content=content, status="체결완료", signed_date=today().isoformat())
    execute("UPDATE hr_sign_requests SET status='완료', signed_at=?, signer_ip=?, signer_ua=?, session_hash=NULL WHERE id=?",
            (signed_at, ip, agent[:200], req["id"]))
    audit("hr", "전자서명 완료", f"doc#{doc['id']}", ip)
    ok, _ = services.store_document_pdf(doc["id"])
    delivered = False
    if ok and mailer.configured():
        from app import storage
        stored = repo.get_document(doc["id"])
        try:
            pdf = storage.read_verified(stored["pdf_path"], stored["pdf_sha256"])
            mailer.send(req["email"], f"[{get_settings().get('company_name', '')}] 체결된 계약서 사본",
                        f"{doc['name']}님, 서명이 완료되어 근로기준법 제17조에 따라 계약서 사본을 보내드립니다.",
                        [(f"{doc['category']}_{doc['name']}.pdf", pdf)])
            delivered = True
            audit("hr", "계약서 사본 교부", f"doc#{doc['id']}", "메일")
        except (mailer.MailError, storage.StorageError) as ex:
            log.warning("사본 교부 실패 doc#%s: %s", doc["id"], ex)
    return ok, "서명이 완료되었습니다. " + ("계약서 사본을 메일로 보내드렸습니다." if delivered else "계약서 사본은 담당자가 전달해 드립니다.")


@router.post("/{token}/submit")
async def sign_submit(request: Request, token: str):
    req, problem = _load(token)
    if problem:
        return _message(request, "전자서명", problem)
    if not _verified(request, req):
        return RedirectResponse(f"/sign/{token}", status_code=303)
    form = await request.form()
    png = _signature_png(str(form.get("signature", "")))
    if not png or not form.get("agree"):
        return _message(request, "전자서명", "서명과 동의가 필요합니다. 이전 화면으로 돌아가 다시 시도해 주세요.")
    doc = repo.get_document(req["doc_id"])
    if content_hash(doc["content"]) != req["content_sha256"]:
        return _message(request, "전자서명", "문서가 변경되어 서명할 수 없습니다. 담당자에게 다시 요청해 주세요.")
    _, text = await run_in_threadpool(_finish, req, png, request.state.ip, request.headers.get("user-agent", ""))
    res = _message(request, "서명 완료", text)
    res.delete_cookie(COOKIE, path="/sign")
    return res
