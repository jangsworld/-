"""2단계 인증: 메일 인증번호(기본) + 인증 앱(TOTP, 대체 수단) + 이 PC 30일 기억.

- 메일 인증: 6자리 번호를 사용자에게 등록된 회사 메일로 보냄. 5분 유효, 5번 틀리면 무효,
  15분에 5번까지 다시 받기. DB 에는 (세션 토큰 + 번호)의 해시만 저장.
- 메일을 쓸 수 없으면(메일 설정 없음·사용자 메일 없음) 인증 앱으로 넘어감.
- 이 PC 기억: 2단계 인증을 통과할 때 고르면 30일 동안 같은 PC·브라우저에서 메일 인증을 묻지 않음.
  쿠키에는 무작위 열쇠만, DB 에는 해시만. 비밀번호를 바꾸거나 관리자가 2단계 인증을 초기화하면 모두 해제.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from html import escape as html_escape

from app import mailer
from app.db import audit, execute, get_settings, query, query_one

DEVICE_COOKIE = "bms_dev"
DEVICE_DAYS = 30
CODE_MINUTES = 5
MAX_TRIES = 5
SEND_LIMIT = (5, 15)          # 15분에 5번
METHODS = {"email": "메일 인증번호", "app": "인증 앱"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _h(*parts) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def mask(email: str | None) -> str:
    e = str(email or "")
    m = re.match(r"^(.{1,2})[^@]*(@.*)$", e)
    return (m.group(1) + "***" + m.group(2)) if m else ""


def system_method() -> str:
    return "app" if get_settings().get("mfa_method") == "app" else "email"


def email_ready(user: dict) -> bool:
    return system_method() == "email" and mailer.configured() and bool(EMAIL_RE.match(str(user.get("email") or "")))


def method_for(user: dict) -> str | None:
    """이 사용자가 지금 쓸 2단계 인증: email / app / None(아직 수단 없음 → 앱 등록 필요)."""
    if email_ready(user):
        return "email"
    if user.get("totp_enabled"):
        return "app"
    return None


# ── 메일 인증번호 ──────────────────────────────────────────────────────
class MfaError(Exception):
    pass


MAIL_TAG = "BMS시스템"      # 제목 머리말·보내는 사람 이름 (FUND시스템·CPO 와 같은 형식)


def code_mail(name: str, code: str, ip: str, at: str) -> tuple[str, str]:
    """인증번호 메일 본문 (일반 텍스트, HTML). 제목에 번호가 있어 폰 알림에서 바로 보임."""
    warn = "본인이 로그인한 것이 아니면 비밀번호를 바꾸고 관리자에게 알려 주세요."
    text = (f"{name}님, 로그인 인증번호는\n\n    {code}\n\n입니다. {CODE_MINUTES}분 안에 입력해 주세요.\n\n"
            f"{warn}\n접속 위치: {ip} · {at}")
    e = html_escape
    page = (
        '<div style="font-family:\'Malgun Gothic\',\'Apple SD Gothic Neo\',sans-serif;font-size:15px;color:#222;line-height:1.6">'
        f'<p style="margin:0 0 8px">{e(name)}님, 로그인 인증번호는</p>'
        f'<p style="margin:12px 0;font-size:32px;font-weight:700;letter-spacing:8px;color:#111">{e(code)}</p>'
        f'<p style="margin:0 0 16px">입니다. {CODE_MINUTES}분 안에 입력해 주세요.</p>'
        f'<p style="margin:0 0 8px;color:#c62828">{e(warn)}</p>'
        f'<p style="margin:0;font-size:12px;color:#888">접속 위치: {e(ip)} · {e(at)}</p></div>')
    return text, page


def send_code(user: dict, sess: dict, token: str, ip: str) -> str:
    if not email_ready(user):
        raise MfaError("메일 인증을 쓸 수 없습니다. 인증 앱으로 확인하거나 관리자에게 메일 주소 등록을 요청하세요.")
    now = datetime.now()
    sends = [s for s in str(sess.get("mail_sends") or "").split(",") if s and s > _ts(now - timedelta(minutes=SEND_LIMIT[1]))]
    if len(sends) >= SEND_LIMIT[0]:
        raise MfaError(f"인증번호를 너무 자주 요청했습니다. {SEND_LIMIT[1]}분 뒤에 다시 해 주세요.")
    if sends and sends[-1] > _ts(now - timedelta(seconds=30)):
        raise MfaError("방금 보냈습니다. 메일함(스팸함 포함)을 확인하고, 30초 뒤에 다시 받을 수 있습니다.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    body, html = code_mail(user["display_name"], code, ip, _ts(now))
    try:
        mailer.send(user["email"], f"[{MAIL_TAG}] 로그인 인증번호 {code}", body, html=html, from_name=MAIL_TAG, plain=True)
    except mailer.MailError as ex:
        audit("auth", "로그인 인증 메일 실패", user["username"], str(ex)[:60])
        raise MfaError("인증 메일을 보내지 못했습니다. 인증 앱으로 확인하거나 관리자에게 알려 주세요.") from ex
    sends.append(_ts(now))
    execute("UPDATE sessions SET mail_code=?, mail_exp=?, mail_tries='0', mail_sends=? WHERE token_hash=?",
            (_h(token, code), _ts(now + timedelta(minutes=CODE_MINUTES)), ",".join(sends), sess["token_hash"]))
    audit("auth", "로그인 인증 메일 발송", user["username"], mask(user["email"]))
    return mask(user["email"])


def verify_code(user: dict, sess: dict, token: str, code: str) -> tuple[bool, str]:
    code = re.sub(r"\D", "", code or "")
    s = query_one("SELECT mail_code, mail_exp, mail_tries FROM sessions WHERE token_hash=?", (sess["token_hash"],)) or {}
    if not s.get("mail_code") or (s.get("mail_exp") or "") < _ts(datetime.now()):
        return False, "인증번호가 없거나 만료되었습니다. [인증번호 다시 받기]를 눌러 주세요."
    tries = int(s.get("mail_tries") or 0)
    if tries >= MAX_TRIES:
        execute("UPDATE sessions SET mail_code=NULL WHERE token_hash=?", (sess["token_hash"],))
        return False, f"인증번호를 {MAX_TRIES}번 틀려 무효로 했습니다. 다시 받아 주세요."
    if len(code) != 6 or not hmac.compare_digest(_h(token, code), s["mail_code"]):
        execute("UPDATE sessions SET mail_tries=? WHERE token_hash=?", (str(tries + 1), sess["token_hash"]))
        left = MAX_TRIES - tries - 1
        return False, f"인증번호가 맞지 않습니다 (남은 시도 {left}번)" if left else "인증번호가 맞지 않습니다. 다시 받아 주세요."
    execute("UPDATE sessions SET mail_code=NULL, mail_exp=NULL, mail_tries=NULL WHERE token_hash=?", (sess["token_hash"],))
    return True, ""


# ── 이 PC 기억 ──────────────────────────────────────────────────────────
def remember(user: dict, ip: str, ua: str) -> str:
    raw = secrets.token_urlsafe(32)
    now = datetime.now()
    execute("INSERT INTO mfa_devices(user_id, token_hash, expires, ip, user_agent, created_at) VALUES (?,?,?,?,?,?)",
            (user["id"], _h(raw), _ts(now + timedelta(days=DEVICE_DAYS)), ip, (ua or "")[:200], _ts(now)))
    audit("auth", f"이 PC {DEVICE_DAYS}일 기억", user["username"], ip)
    return raw


def remembered(user: dict, raw: str | None) -> bool:
    if not raw or len(raw) > 100:
        return False
    r = query_one("SELECT id FROM mfa_devices WHERE user_id=? AND token_hash=? AND expires>?",
                  (user["id"], _h(raw), _ts(datetime.now())))
    return bool(r)


def devices(uid: int) -> list[dict]:
    return query("SELECT id, ip, user_agent, created_at, expires FROM mfa_devices WHERE user_id=? AND expires>? ORDER BY id DESC",
                 (uid, _ts(datetime.now())))


def forget_devices(uid: int) -> int:
    n = len(devices(uid))
    execute("DELETE FROM mfa_devices WHERE user_id=?", (uid,))
    return n


def purge():
    execute("DELETE FROM mfa_devices WHERE expires<?", (_ts(datetime.now()),))
