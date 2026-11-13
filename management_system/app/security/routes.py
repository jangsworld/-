"""로그인·계정·사용자관리·접속기록 화면."""
import csv
import io
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.common import templates
from app.config import settings
from app.db import audit, execute, query
from app.modules.hr import repo
from app.security import auth, mfa

router = APIRouter()


def _r(request, name, ctx=None, status=200):
    ctx = ctx or {}
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def _go(url, msg=""):
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def _safe_next(nxt: str | None) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return "/"


def _set_cookie(resp, token):
    resp.set_cookie(auth.COOKIE_NAME, token, httponly=True, secure=settings.secure_cookie,
                    samesite="strict", path="/")
    return resp


@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "system": "BMS"}


# ── 로그인 ──────────────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", ended: str = ""):
    if request.state.user and request.state.session["mfa_passed"]:
        return _go(_safe_next(next))
    notice = "접속이 종료되었습니다. 같은 계정이 다른 곳에서 로그인했거나 오래 사용하지 않았습니다" if ended else ""
    return _r(request, "auth/login.html", {"next": _safe_next(next), "notice": notice})


@router.post("/login")
async def login(request: Request):
    f = await request.form()
    user, err = auth.authenticate(str(f.get("username", "")), str(f.get("password", "")), request.state.ip)
    nxt = _safe_next(str(f.get("next", "/")))
    if not user:
        audit("auth", "로그인 실패", str(f.get("username", ""))[:30], err[:40])
        return _r(request, "auth/login.html", {"error": err, "next": nxt, "username": f.get("username", "")}, 401)
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))  # 세션 고정 공격 방지
    method = mfa.method_for(user)
    mfa_needed = bool(method) or settings.require_2fa
    skip = mfa_needed and bool(method) and mfa.remembered(user, request.cookies.get(mfa.DEVICE_COOKIE))
    passed = (not mfa_needed) or skip
    token = auth.create_session(user, request.state.ip, request.headers.get("user-agent", ""), passed)
    if passed:
        auth.revoke_sessions(user["id"], except_token=token)    # 계정당 한 곳만 접속
    execute("UPDATE users SET last_login_at=datetime('now','localtime'), last_login_ip=? WHERE id=?",
            (request.state.ip, user["id"]))
    audit("auth", "로그인 성공(기억한 PC, 2단계 생략)" if skip else "로그인(1단계)", user["username"], "")
    if passed:
        return _set_cookie(_go(nxt), token)
    if not method:
        return _set_cookie(_go("/account/2fa"), token)
    msg = ""
    if method == "email":
        sess = {"token_hash": auth._hash_token(token), "mail_sends": ""}
        try:
            msg = f"{mfa.send_code(user, sess, token, request.state.ip)} 로 인증번호를 보냈습니다"
        except mfa.MfaError as ex:
            msg = str(ex)
    return _set_cookie(_go("/login/2fa?next=" + quote(nxt), msg), token)


def _mfa_ctx(user, nxt, use=""):
    method = mfa.method_for(user)
    if use == "app" and user["totp_enabled"]:
        method = "app"
    return {"next": nxt, "method": method, "to": mfa.mask(user.get("email")), "has_app": bool(user["totp_enabled"]),
            "email_ok": mfa.email_ready(user), "days": mfa.DEVICE_DAYS, "minutes": mfa.CODE_MINUTES}


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, next: str = "/", use: str = ""):
    if not request.state.user:
        return _go("/login")
    if request.state.session["mfa_passed"]:
        return _go(_safe_next(next))
    return _r(request, "auth/login_2fa.html", _mfa_ctx(request.state.user, _safe_next(next), use))


@router.post("/login/2fa/send")
async def login_2fa_send(request: Request):
    user, sess = request.state.user, request.state.session
    if not user:
        return _go("/login")
    f = await request.form()
    nxt = _safe_next(str(f.get("next", "/")))
    try:
        to = mfa.send_code(user, sess, request.cookies.get(auth.COOKIE_NAME), request.state.ip)
        msg = f"{to} 로 인증번호를 다시 보냈습니다"
    except mfa.MfaError as ex:
        msg = str(ex)
    return _go("/login/2fa?next=" + quote(nxt), msg)


@router.post("/login/2fa")
async def login_2fa(request: Request):
    user, sess = request.state.user, request.state.session
    if not user:
        return _go("/login")
    f = await request.form()
    nxt = _safe_next(str(f.get("next", "/")))
    token = request.cookies.get(auth.COOKIE_NAME)
    use = "app" if (str(f.get("method")) == "app" and user["totp_enabled"]) else (mfa.method_for(user) or "")
    if use == "email":
        ok, err = mfa.verify_code(user, sess, token, str(f.get("code", "")))
    elif use == "app":
        ok, err = auth.verify_totp(user, str(f.get("code", ""))), "인증 앱 코드가 올바르지 않습니다"
    else:
        return _go("/account/2fa")
    if ok:
        auth.mark_mfa_passed(token)
        auth.revoke_sessions(user["id"], except_token=token)    # 계정당 한 곳만 접속
        execute("UPDATE users SET failed_count=0 WHERE id=?", (user["id"],))
        audit("auth", f"로그인 성공(2단계·{mfa.METHODS[use]})", user["username"], "")
        resp = _go(nxt)
        if f.get("remember"):
            raw = mfa.remember(user, request.state.ip, request.headers.get("user-agent", ""))
            resp.set_cookie(mfa.DEVICE_COOKIE, raw, max_age=mfa.DEVICE_DAYS * 86400, httponly=True,
                            secure=settings.secure_cookie, samesite="strict", path="/")
        return resp
    fails = user["failed_count"] + 1
    execute("UPDATE users SET failed_count=? WHERE id=?", (fails, user["id"]))
    audit("auth", "2단계 인증 실패", user["username"], f"{mfa.METHODS[use]} {fails}회")
    if fails >= settings.max_failed_logins:
        execute("UPDATE users SET failed_count=0, locked_until=datetime('now','localtime', ?) WHERE id=?",
                (f"+{settings.lockout_minutes} minutes", user["id"]))
        auth.revoke_sessions(user["id"])
        return _go("/login", "2단계 인증 실패가 반복되어 계정이 잠겼습니다")
    ctx = _mfa_ctx(user, nxt, use)
    ctx["error"] = err
    return _r(request, "auth/login_2fa.html", ctx, 401)


@router.post("/logout")
def logout(request: Request):
    if request.state.user:
        audit("auth", "로그아웃", request.state.user["username"], "")
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))
    resp = _go("/login", "로그아웃되었습니다")
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# ── 내 계정 ─────────────────────────────────────────────────────────────
@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    u = request.state.user
    sessions = query("SELECT ip, user_agent, created_at, last_seen FROM sessions WHERE user_id=? ORDER BY last_seen DESC",
                     (u["id"],))
    logins = query("SELECT ip, success, reason, created_at FROM login_attempts WHERE username=? ORDER BY id DESC LIMIT 10",
                   (u["username"],))
    return _r(request, "auth/account.html", {"u": u, "sessions": sessions, "logins": logins,
                                             "roles": auth.ROLES, "active_menu": "account",
                                             "mfa_method": mfa.method_for(u), "methods": mfa.METHODS,
                                             "mail_to": mfa.mask(u.get("email")), "devices": mfa.devices(u["id"])})


@router.post("/account/devices/clear")
def account_devices_clear(request: Request):
    u = request.state.user
    n = mfa.forget_devices(u["id"])
    audit("auth", "기억한 PC 모두 해제", u["username"], f"{n}대")
    resp = _go("/account", f"기억한 PC {n}대를 해제했습니다. 다음 로그인부터 다시 인증합니다")
    resp.delete_cookie(mfa.DEVICE_COOKIE, path="/")
    return resp


@router.post("/account/logout-others")
def logout_others(request: Request):
    auth.revoke_sessions(request.state.user["id"], except_token=request.cookies.get(auth.COOKIE_NAME))
    audit("auth", "다른 기기 로그아웃", request.state.user["username"], "")
    return _go("/account", "다른 기기의 세션을 모두 종료했습니다")


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request):
    u = request.state.user
    return _r(request, "auth/password.html", {"u": u, "forced": bool(u["must_change_pw"]) or auth.password_expired(u),
                                              "min_len": settings.password_min_length})


@router.post("/account/password")
async def password_change(request: Request):
    u = request.state.user
    f = await request.form()
    cur, new, new2 = str(f.get("current", "")), str(f.get("new", "")), str(f.get("new2", ""))
    err = None
    if not auth.verify_password(u["password_hash"], cur):
        err = "현재 비밀번호가 올바르지 않습니다"
    elif new != new2:
        err = "새 비밀번호가 일치하지 않습니다"
    elif new == cur:
        err = "현재와 다른 비밀번호를 사용하세요"
    else:
        probs = auth.password_problems(new, u["username"])
        err = " / ".join(probs) if probs else None
    if err:
        return _r(request, "auth/password.html", {"u": u, "error": err, "forced": bool(u["must_change_pw"]),
                                                  "min_len": settings.password_min_length}, 400)
    auth.set_password(u["id"], new)  # 모든 세션 폐기
    mfa.forget_devices(u["id"])      # 기억한 PC도 모두 해제
    audit("auth", "비밀번호 변경", u["username"], "")
    import os
    init_file = os.path.join(settings.secrets_dir, "initial_admin_password.txt")
    if u["username"] == "admin" and os.path.exists(init_file):
        os.remove(init_file)
    token = auth.create_session(u, request.state.ip, request.headers.get("user-agent", ""), True)
    return _set_cookie(_go("/", "비밀번호가 변경되었습니다"), token)


@router.get("/account/2fa", response_class=HTMLResponse)
def twofa_page(request: Request):
    u = request.state.user
    if u["totp_enabled"] and request.state.session["mfa_passed"]:
        return _r(request, "auth/twofa.html", {"enabled": True, "u": u})
    secret = u.get("totp_secret")
    if not secret or u["totp_enabled"]:
        secret = auth.new_totp_secret()
        from app.security.crypto import enc
        execute("UPDATE users SET totp_secret=?, totp_enabled=0 WHERE id=?", (enc(secret), u["id"]))
    return _r(request, "auth/twofa.html", {"enabled": False, "u": u, "secret": secret,
                                           "uri": auth.totp_uri(u["username"], secret),
                                           "forced": not request.state.session["mfa_passed"]})


@router.post("/account/2fa")
async def twofa_enable(request: Request):
    u = request.state.user
    f = await request.form()
    if u["totp_enabled"] and request.state.session["mfa_passed"]:
        return _go("/account")
    if not auth.verify_totp(u, str(f.get("code", "")), u.get("totp_secret")):
        return _go("/account/2fa", "인증코드가 올바르지 않습니다. 휴대폰 시간이 자동 설정인지 확인하세요")
    auth.enable_totp(u["id"], u["totp_secret"])
    auth.mark_mfa_passed(request.cookies.get(auth.COOKIE_NAME))
    auth.revoke_sessions(u["id"], except_token=request.cookies.get(auth.COOKIE_NAME))
    audit("auth", "2단계 인증 등록", u["username"], "")
    return _go("/", "2단계 인증이 등록되었습니다")


# ── 사용자 관리 (관리자) ────────────────────────────────────────────────
# 임시 비밀번호 1회 표시용 (서버 메모리, URL·로그에 남기지 않음)
_one_time: dict[int, tuple[str, str]] = {}


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request):
    from app import mailer
    temp_user, temp = _one_time.pop(request.state.user["id"], (None, None))
    users = auth.list_users()
    for x in users:
        x["mfa_now"] = mfa.method_for(x)
        x["devices"] = len(mfa.devices(x["id"]))
    return _r(request, "auth/users.html", {"users": users, "roles": auth.ROLES, "active_menu": "admin.users",
                                           "temp": temp, "temp_user": temp_user, "methods": mfa.METHODS,
                                           "mfa_method": mfa.system_method(), "mail_ok": mailer.configured(),
                                           "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@router.post("/admin/mfa-method")
async def mfa_method_save(request: Request):
    from app import mailer
    from app.db import set_setting
    f = await request.form()
    m = "app" if f.get("method") == "app" else "email"
    if m == "email" and not mailer.configured():
        return _go("/admin/users", "메일 발송 설정(회사정보·환경설정 → 메일)을 먼저 해 주세요")
    set_setting("mfa_method", m)
    audit("auth", "2단계 인증 방식 변경", "", mfa.METHODS[m])
    return _go("/admin/users", f"2단계 인증 방식을 '{mfa.METHODS[m]}'(으)로 바꿨습니다")


def _temp_redirect(request, username, pw, msg):
    _one_time[request.state.user["id"]] = (username, pw)
    return _go("/admin/users", msg)


@router.post("/admin/users")
async def users_create(request: Request):
    f = await request.form()
    pw = auth.generate_temp_password()
    email = str(f.get("email", "")).strip()
    if email and not mfa.EMAIL_RE.match(email):
        return _go("/admin/users", "메일 주소 형식이 아닙니다")
    try:
        uid = auth.create_user(str(f.get("username", "")).strip(), str(f.get("display_name", "")).strip() or str(f.get("username")),
                               str(f.get("role", "viewer")), pw)
    except Exception as ex:
        return _go("/admin/users", f"생성 실패: {ex}")
    if email:
        execute("UPDATE users SET email=? WHERE id=?", (email, uid))
    return _temp_redirect(request, f["username"], pw, "사용자가 생성되었습니다")


def _guard_self(request, uid):
    return request.state.user["id"] == uid


@router.post("/admin/users/{uid}/reset-password")
def users_reset_pw(request: Request, uid: int):
    u = auth.get_user(uid)
    pw = auth.generate_temp_password()
    auth.set_password(uid, pw, must_change=True)
    audit("auth", "비밀번호 초기화", u["username"], "관리자")
    return _temp_redirect(request, u["username"], pw, "임시 비밀번호가 발급되었습니다")


@router.post("/admin/users/{uid}/reset-2fa")
def users_reset_2fa(request: Request, uid: int):
    u = auth.get_user(uid)
    auth.reset_totp(uid)
    mfa.forget_devices(uid)
    auth.revoke_sessions(uid)
    audit("auth", "2단계 인증 초기화", u["username"], "관리자")
    return _go("/admin/users", f"{u['username']}의 인증 앱·기억한 PC를 초기화했습니다")


@router.post("/admin/users/{uid}/unlock")
def users_unlock(uid: int):
    u = auth.get_user(uid)
    execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE id=?", (uid,))
    audit("auth", "계정 잠금 해제", u["username"], "")
    return _go("/admin/users", "잠금이 해제되었습니다")


@router.post("/admin/users/{uid}/update")
async def users_update(request: Request, uid: int):
    f = await request.form()
    u = auth.get_user(uid)
    role, active = str(f.get("role", u["role"])), 1 if f.get("is_active") else 0
    if _guard_self(request, uid) and (role != "admin" or not active):
        return _go("/admin/users", "본인 계정의 관리자 권한·사용 상태는 변경할 수 없습니다")
    if role not in auth.ROLES:
        return _go("/admin/users", "잘못된 권한")
    email = str(f.get("email", u.get("email") or "")).strip()
    if email and not mfa.EMAIL_RE.match(email):
        return _go("/admin/users", "메일 주소 형식이 아닙니다")
    if email != (u.get("email") or ""):
        mfa.forget_devices(uid)           # 인증 메일 주소가 바뀌면 기억한 PC 해제
        audit("auth", "2단계 인증 메일 변경", u["username"], mfa.mask(email))
    execute("UPDATE users SET role=?, is_active=?, display_name=?, email=? WHERE id=?",
            (role, active, str(f.get("display_name") or u["display_name"]), email or None, uid))
    if not active or role != u["role"]:
        auth.revoke_sessions(uid)
    audit("auth", "사용자 수정", u["username"], f"{auth.ROLES[role]} / {'사용' if active else '중지'}")
    return _go("/admin/users", "저장되었습니다")


# ── 접속기록 (관리자) ──────────────────────────────────────────────────
@router.get("/admin/audit", response_class=HTMLResponse)
def audit_page(request: Request, q: str = "", date_from: str = "", date_to: str = ""):
    logs = repo.recent_audit(500, q, date_from, date_to)
    attempts = query("SELECT * FROM login_attempts WHERE success=0 ORDER BY id DESC LIMIT 50")
    return _r(request, "auth/audit.html", {"logs": logs, "attempts": attempts, "active_menu": "admin.audit",
                                           "f": {"q": q, "date_from": date_from, "date_to": date_to}})


@router.get("/admin/audit/export.csv")
def audit_export(q: str = "", date_from: str = "", date_to: str = ""):
    logs = repo.recent_audit(100000, q, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["일시", "사용자", "IP", "모듈", "작업", "대상", "내용"])
    for l in logs:
        w.writerow([l["created_at"], l["username"], l["ip"], l["module"], l["action"], l["target"], l["detail"]])
    audit("auth", "접속기록 내보내기", "", f"{len(logs)}건")
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote("BMS_접속기록.csv")})
