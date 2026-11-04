"""인증: 비밀번호(Argon2id), 세션(서버 저장·토큰 해시), 2단계 인증(TOTP), 계정 잠금."""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from app.config import settings, SYSTEM_CODE
from app.db import query, query_one, execute, audit
from app.security.crypto import enc

COOKIE_NAME = "bms_session"
ROLES = {"admin": "시스템관리자", "hr": "인사·총무담당", "ga": "총무담당", "viewer": "조회자"}

_ph = PasswordHasher()  # Argon2id, RFC 9106 권장 파라미터
_DUMMY_HASH = _ph.hash(secrets.token_hex(16))  # 계정 존재 여부 타이밍 노출 방지용


def _now() -> datetime:
    return datetime.now()


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


# ════════════════════════════════════════════════════════════════════════
# 비밀번호
# ════════════════════════════════════════════════════════════════════════
def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_problems(pw: str, username: str = "") -> list[str]:
    """비밀번호 정책 검사. 문제 목록 반환(비어 있으면 통과)."""
    p = []
    if len(pw) < settings.password_min_length:
        p.append(f"{settings.password_min_length}자 이상이어야 합니다")
    kinds = sum(bool(re.search(r, pw)) for r in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if kinds < 3:
        p.append("영문 대문자·소문자·숫자·특수문자 중 3종류 이상을 사용하세요")
    if username and username.lower() in pw.lower():
        p.append("아이디를 포함할 수 없습니다")
    if re.search(r"(.)\1\1\1", pw):
        p.append("같은 문자를 4번 이상 반복할 수 없습니다")
    return p


def generate_temp_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    core = "".join(secrets.choice(alphabet) for _ in range(12))
    return core[:4] + "-" + core[4:8] + "-" + core[8:] + "!"


# ════════════════════════════════════════════════════════════════════════
# 사용자
# ════════════════════════════════════════════════════════════════════════
def count_users() -> int:
    return query_one("SELECT COUNT(*) c FROM users")["c"]


def get_user(uid: int) -> dict | None:
    return query_one("SELECT * FROM users WHERE id=?", (uid,))


def list_users() -> list[dict]:
    return query("SELECT id, username, display_name, role, is_active, totp_enabled, must_change_pw, "
                 "failed_count, locked_until, last_login_at, last_login_ip, pw_changed_at, created_at "
                 "FROM users ORDER BY id")


def create_user(username: str, display_name: str, role: str, password: str, must_change=True) -> int:
    if role not in ROLES:
        raise ValueError("잘못된 권한입니다")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", username):
        raise ValueError("아이디는 영문·숫자·._- 3~30자")
    uid = execute(
        "INSERT INTO users(username, display_name, role, password_hash, must_change_pw, pw_changed_at) "
        "VALUES (?,?,?,?,?,?)",
        (username, display_name, role, hash_password(password), 1 if must_change else 0, _ts(_now())),
    )
    audit("auth", "사용자 생성", username, ROLES[role])
    return uid


def set_password(uid: int, password: str, must_change=False):
    execute("UPDATE users SET password_hash=?, must_change_pw=?, pw_changed_at=?, failed_count=0, "
            "locked_until=NULL WHERE id=?", (hash_password(password), 1 if must_change else 0, _ts(_now()), uid))
    revoke_sessions(uid)


def password_expired(user: dict) -> bool:
    if not settings.password_max_age_days or not user.get("pw_changed_at"):
        return False
    changed = datetime.strptime(user["pw_changed_at"][:19], "%Y-%m-%d %H:%M:%S")
    return _now() - changed > timedelta(days=settings.password_max_age_days)


# ════════════════════════════════════════════════════════════════════════
# 로그인 / 잠금
# ════════════════════════════════════════════════════════════════════════
def _ip_blocked(ip: str) -> bool:
    """IP 기준 무차별 대입 차단: 15분 내 실패 20회 이상."""
    since = _ts(_now() - timedelta(minutes=15))
    n = query_one("SELECT COUNT(*) c FROM login_attempts WHERE ip=? AND success=0 AND created_at>=?",
                  (ip, since))["c"]
    return n >= 20


def _log_attempt(ip, username, success, reason=""):
    execute("INSERT INTO login_attempts(ip, username, success, reason, created_at) VALUES (?,?,?,?,?)",
            (ip, username[:50], int(success), reason, _ts(_now())))


def authenticate(username: str, password: str, ip: str) -> tuple[dict | None, str]:
    """(user, 오류메시지). 오류 메시지는 계정 존재 여부를 노출하지 않도록 통일."""
    generic = "아이디 또는 비밀번호가 올바르지 않습니다"
    if _ip_blocked(ip):
        _log_attempt(ip, username, False, "ip_blocked")
        return None, "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요"
    user = query_one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not user:
        verify_password(_DUMMY_HASH, password)
        _log_attempt(ip, username, False, "no_user")
        return None, generic
    if user["locked_until"] and user["locked_until"] > _ts(_now()):
        _log_attempt(ip, username, False, "locked")
        return None, f"계정이 잠겨 있습니다 ({user['locked_until'][11:16]} 이후 재시도 또는 관리자 문의)"
    if not verify_password(user["password_hash"], password):
        fails = user["failed_count"] + 1
        locked = _ts(_now() + timedelta(minutes=settings.lockout_minutes)) if fails >= settings.max_failed_logins else None
        execute("UPDATE users SET failed_count=?, locked_until=? WHERE id=?",
                (0 if locked else fails, locked, user["id"]))
        _log_attempt(ip, username, False, "bad_password")
        if locked:
            audit("auth", "계정 잠금", username, f"{settings.max_failed_logins}회 실패")
            return None, f"비밀번호 {settings.max_failed_logins}회 오류로 계정이 {settings.lockout_minutes}분간 잠겼습니다"
        return None, f"{generic} (남은 시도 {settings.max_failed_logins - fails}회)"
    if not user["is_active"]:
        _log_attempt(ip, username, False, "inactive")
        return None, generic
    if _ph.check_needs_rehash(user["password_hash"]):
        execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user["id"]))
    execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE id=?", (user["id"],))
    _log_attempt(ip, username, True)
    return user, ""


# ════════════════════════════════════════════════════════════════════════
# 세션 (쿠키에는 무작위 토큰만, DB 에는 해시만 저장)
# ════════════════════════════════════════════════════════════════════════
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user: dict, ip: str, ua: str, mfa_passed: bool) -> str:
    token = secrets.token_urlsafe(32)
    now = _ts(_now())
    execute("INSERT INTO sessions(token_hash, user_id, csrf_token, mfa_passed, ip, user_agent, created_at, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_hash_token(token), user["id"], secrets.token_urlsafe(32), int(mfa_passed), ip, ua[:200], now, now))
    return token


def load_session(token: str | None) -> tuple[dict | None, dict | None]:
    """유효한 세션이면 (session, user). 유휴·최대 시간 초과 시 폐기."""
    if not token:
        return None, None
    th = _hash_token(token)
    s = query_one("SELECT * FROM sessions WHERE token_hash=?", (th,))
    if not s:
        return None, None
    now = _now()
    last = datetime.strptime(s["last_seen"], "%Y-%m-%d %H:%M:%S")
    created = datetime.strptime(s["created_at"], "%Y-%m-%d %H:%M:%S")
    if now - last > timedelta(minutes=settings.session_idle_minutes) or \
            now - created > timedelta(hours=settings.session_max_hours):
        execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        return None, None
    user = get_user(s["user_id"])
    if not user or not user["is_active"]:
        execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        return None, None
    if (now - last).total_seconds() > 30:  # 쓰기 빈도 절감
        execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (_ts(now), th))
    return s, user


def mark_mfa_passed(token: str):
    execute("UPDATE sessions SET mfa_passed=1 WHERE token_hash=?", (_hash_token(token),))


def destroy_session(token: str | None):
    if token:
        execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def revoke_sessions(uid: int, except_token: str | None = None):
    if except_token:
        execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (uid, _hash_token(except_token)))
    else:
        execute("DELETE FROM sessions WHERE user_id=?", (uid,))


def purge_expired():
    cutoff = _ts(_now() - timedelta(hours=settings.session_max_hours))
    execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
    execute("DELETE FROM login_attempts WHERE created_at < ?", (_ts(_now() - timedelta(days=400)),))


# ════════════════════════════════════════════════════════════════════════
# TOTP (RFC 6238) – Google/Microsoft Authenticator 호환
# ════════════════════════════════════════════════════════════════════════
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, t: float | None = None) -> str:
    return _hotp(secret, int((t or time.time()) // 30))


def verify_totp(user: dict, code: str, secret: str | None = None) -> bool:
    """±1 스텝(30초) 허용, 이미 사용한 스텝은 재사용 불가."""
    secret = secret or user.get("totp_secret")
    code = re.sub(r"\D", "", code or "")
    if not secret or len(code) != 6:
        return False
    step = int(time.time() // 30)
    for s in (step - 1, step, step + 1):
        if hmac.compare_digest(_hotp(secret, s), code):
            if user.get("totp_last_step") and s <= user["totp_last_step"]:
                return False
            execute("UPDATE users SET totp_last_step=? WHERE id=?", (s, user["id"]))
            return True
    return False


def enable_totp(uid: int, secret: str):
    execute("UPDATE users SET totp_secret=?, totp_enabled=1 WHERE id=?", (enc(secret), uid))


def reset_totp(uid: int):
    execute("UPDATE users SET totp_secret=NULL, totp_enabled=0, totp_last_step=NULL WHERE id=?", (uid,))


def totp_uri(username: str, secret: str) -> str:
    issuer = f"{SYSTEM_CODE} 경영관리"
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
