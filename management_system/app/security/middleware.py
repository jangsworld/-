"""보안 미들웨어: 인증 강제, 2단계 인증·비밀번호 변경 강제, 권한(RBAC), CSRF, 보안 헤더."""
import hmac
import json
import re
import secrets
from urllib.parse import parse_qs, quote, urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from app.config import settings
from app.db import current_actor
from app.security import auth

PUBLIC_PREFIXES = ("/static/", "/login", "/healthz", "/favicon.ico", "/sign/",  # /sign/: 직원 전자서명 (토큰+본인확인)
                   "/q/",            # /q/: 자산 라벨 QR 기본 정보 (무작위 열쇠, 금액·이력 제외)
                   "/leave-plan/",   # /leave-plan/: 직원 연차 사용계획 회신 (토큰+생년월일)
                   "/vq/")           # /vq/: 차량 QR — 점검 사진·파손 신고 (무작위 열쇠 + 이름·생년월일)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# 경로별 필요 권한 (위에서부터 첫 일치 규칙 적용)
ADMIN = {"admin"}
WRITERS = {"admin", "hr"}                  # 인사 쓰기
GA_STAFF = {"admin", "hr", "ga"}           # 총무 쓰기 (경영지원 인사·총무 겸임 고려)
ALL = {"admin", "hr", "ga", "viewer"}
RULES = [
    (r"^/admin", ADMIN, ADMIN),
    (r"^/settings", ADMIN, ADMIN),
    (r"^/account", ALL, ALL),
    (r"^/logout", ALL, ALL),
    # 연봉·계약서·증명서·상벌·근로특이사항(건강·가족정보)·내보내기: 조회자 접근 불가
    (r"^/hr/(leave|ecount|salaries|contracts|documents|templates|certificates|rewards|specials|drafts|technicians|api)", WRITERS, WRITERS),
    (r"^/hr/employees/(new|import|export\.csv|template\.csv|\d+/(edit|file|checks))", WRITERS, WRITERS),
    (r"^/hr", ALL, WRITERS),
    # 총무: 관인 승인은 관리자, 경조사(가족 사망·질병 등)는 총무 담당자 이상만 열람
    (r"^/ga/seals/\d+/decide", ADMIN, ADMIN),
    (r"^/ga/events", GA_STAFF, GA_STAFF),
    (r"^/ga/(assets|contracts)/export\.csv", GA_STAFF, GA_STAFF),
    (r"^/ga/(contract-drafts|contracts/\d+/file)", GA_STAFF, GA_STAFF),     # 계약서 원본·초안: 총무 담당자 이상
    (r"^/ga", ALL, GA_STAFF),
    # 경영관리: 교육 이수·법정의무 일정은 총무 담당 이상, 인건비·인사평가·인센티브는 관리자·인사담당만
    (r"^/mgmt/(training|compliance)", GA_STAFF, GA_STAFF),
    (r"^/mgmt", WRITERS, WRITERS),
    (r"^/$", ALL, ALL),
]


def required_roles(path: str, method: str) -> set:
    for pat, read_roles, write_roles in RULES:
        if re.match(pat, path):
            return read_roles if method in SAFE_METHODS else write_roles
    return ADMIN  # 규칙에 없는 경로는 기본 거부(관리자만)


def _proxied(request: Request) -> bool:
    """같은 서버의 프록시(예: Tailscale HTTPS)를 거쳐 들어온 요청인지. 사내 직접 접속은 False."""
    peer = request.client.host if request.client else ""
    if peer not in settings.trusted_proxies:
        return False
    return settings.behind_proxy or bool(request.headers.get("x-forwarded-for") or request.headers.get("x-forwarded-proto"))


def is_https(request: Request) -> bool:
    """이 요청이 브라우저↔서버 구간에서 HTTPS 인지 (요청마다 판단 → 사내 http 접속은 그대로 동작)."""
    if settings.ssl_certfile or request.url.scheme == "https":
        return True
    return _proxied(request) and request.headers.get("x-forwarded-proto", "").lower() == "https"


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if _proxied(request):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def _is_api(request: Request) -> bool:
    return "/api/" in request.url.path or request.headers.get("content-type", "").startswith("application/json")


def _deny(request: Request, status: int, msg: str) -> Response:
    if _is_api(request):
        return JSONResponse({"ok": False, "message": msg}, status_code=status)
    if status == 401:
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        ended = "&ended=1" if request.cookies.get(auth.COOKIE_NAME) else ""    # 쿠키는 있는데 세션이 끝난 경우 안내
        return RedirectResponse("/login?next=" + quote(nxt) + ended, status_code=303)
    from app.common import templates
    return templates.TemplateResponse(request, "error.html", {"status": status, "message": msg}, status_code=status)


def _origin_ok(request: Request) -> bool:
    """CSRF 1차 방어: Origin(없으면 Referer)이 같은 호스트인지 확인."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return False
    own = {request.headers.get("host", "")}
    if _proxied(request):
        own.add(request.headers.get("x-forwarded-host", ""))
    own.discard("")
    return urlparse(src).netloc in own


async def _csrf_token_from(request: Request) -> str:
    hdr = request.headers.get("x-csrf-token")
    if hdr:
        return hdr
    ctype = request.headers.get("content-type", "")
    body = await request.body()  # Starlette 가 캐시하므로 이후 핸들러에서도 읽을 수 있음
    if ctype.startswith("application/x-www-form-urlencoded"):
        return (parse_qs(body.decode("utf-8", "ignore")).get("csrf_token") or [""])[0]
    if ctype.startswith("multipart/form-data"):
        m = re.search(rb'name="csrf_token"\r\n\r\n([^\r\n]+)', body)
        return m.group(1).decode() if m else ""
    if ctype.startswith("application/json"):
        try:
            return json.loads(body or b"{}").get("csrf_token", "")
        except ValueError:
            return ""
    return ""


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.csp_nonce = secrets.token_urlsafe(16)
        if is_https(request) and request.scope.get("scheme") == "http":
            request.scope["scheme"] = "https"      # 메일 링크·QR 주소도 https 로
        request.state.user = None
        request.state.session = None
        ip = client_ip(request)
        request.state.ip = ip
        path = request.url.path

        # 업로드 크기 제한
        cl = request.headers.get("content-length")
        photo_up = path.startswith(("/ga/assets/", "/vq/")) or (path.startswith("/ga/vehicles/") and path.endswith("/photo"))
        limit_mb = 25 if photo_up else settings.max_upload_mb   # 자산·차량 사진은 서버에서 줄여 저장
        if cl and cl.isdigit() and int(cl) > limit_mb * 1024 * 1024:
            return PlainTextResponse("요청 크기가 너무 큽니다", status_code=413)

        is_public = path.startswith(PUBLIC_PREFIXES)
        token = request.cookies.get(auth.COOKIE_NAME)
        sess, user = auth.load_session(token)
        request.state.user, request.state.session = user, sess
        actor = current_actor.set((user["id"], user["username"], ip) if user else (None, "anonymous", ip))
        try:
            # CSRF: 상태 변경 요청은 Origin 확인 + (로그인 후) 세션 토큰 확인
            if request.method not in SAFE_METHODS:
                if not _origin_ok(request):
                    return _deny(request, 403, "요청 출처를 확인할 수 없습니다 (CSRF 차단)")
                if sess and not hmac.compare_digest(await _csrf_token_from(request), sess["csrf_token"]):
                    return _deny(request, 403, "보안 토큰이 만료되었습니다. 페이지를 새로고침하세요")

            if not is_public:
                if not user:
                    return _deny(request, 401, "로그인이 필요합니다")
                gate = self._gate(user, sess, path)
                if gate:
                    return RedirectResponse(gate, status_code=303) if request.method in SAFE_METHODS \
                        else _deny(request, 403, "추가 인증이 필요합니다")
                if user["role"] not in required_roles(path, request.method):
                    return _deny(request, 403, "이 메뉴에 대한 접근 권한이 없습니다")

            response = await call_next(request)
        finally:
            current_actor.reset(actor)

        self._headers(request, response, path)
        return response

    @staticmethod
    def _gate(user, sess, path) -> str | None:
        """2단계 인증·비밀번호 변경이 끝나기 전에는 해당 화면만 허용."""
        if path.startswith("/logout"):
            return None
        if not sess["mfa_passed"]:
            from app.security import mfa
            if mfa.method_for(user):        # 메일 인증번호 또는 인증 앱
                return None if path.startswith("/login/2fa") else "/login/2fa"
            if settings.require_2fa:
                return None if path.startswith("/account/2fa") else "/account/2fa"
        if user["must_change_pw"] or auth.password_expired(user):
            return None if path.startswith("/account/password") else "/account/password"
        return None

    @staticmethod
    def _headers(request: Request, response: Response, path: str):
        n = request.state.csp_nonce
        h = response.headers
        h["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{n}'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'; object-src 'none'"
        )
        h["X-Frame-Options"] = "DENY"
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        if settings.https or is_https(request):
            h["Strict-Transport-Security"] = "max-age=31536000"
            # HTTPS 로 들어온 요청의 쿠키는 HTTPS 에서만 보내도록
            h.raw[:] = [(k, v + b"; Secure") if k == b"set-cookie" and b"secure" not in v.lower() else (k, v)
                        for k, v in h.raw]
        if not path.startswith("/static/"):
            h["Cache-Control"] = "no-store"  # 개인정보 화면을 브라우저·프록시 캐시에 남기지 않음
            h["Pragma"] = "no-cache"
        if "server" in h:
            del h["server"]
