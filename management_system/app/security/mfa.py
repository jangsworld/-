"""2단계 인증: 메일 인증번호(기본) + 인증 앱(TOTP, 대체 수단). 로그인할 때마다 확인.

- 메일 인증: 6자리 번호를 사용자에게 등록된 회사 메일로 보냄. 3분 유효, 5번 틀리면 무효,
  15분에 5번까지 다시 받기. DB 에는 번호의 서버 비밀 지문만 저장.
- 번호·발송 횟수·실패 횟수는 모두 '계정' 기준(mfa_state): 로그인을 다시 하거나 브라우저를 바꿔도 이어짐.
  · 유효한 번호가 남아 있으면 새로 만들지도 다시 보내지도 않음 (뒤로가기 → 재로그인 때 메일이 또 가지 않게)
  · 2단계 인증 실패는 비밀번호가 맞아도 지워지지 않고, 2단계까지 성공해야 0 이 됨. 15분에 5번이면 계정 잠금.
- 메일을 쓸 수 없으면(메일 설정 없음·사용자 메일 없음) 인증 앱으로 넘어감.
- '이 PC 30일 기억'은 폐지 (2026-11): 예전 쿠키는 로그인 때 지우고, 기억 표(mfa_devices)는 정리할 때 비움.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from html import escape as html_escape

from app import mailer
from app.db import audit, execute, get_settings, query, query_one

OLD_DEVICE_COOKIE = "bms_dev"   # 폐지된 '이 PC 기억' 쿠키 이름 (남아 있으면 지우기만 함)
CODE_MINUTES = 3
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


FAIL_LIMIT = 5                 # 15분 안에 2단계 인증을 이만큼 틀리면 계정 잠금
FAIL_WINDOW_MIN = 15


def _state(uid: int) -> dict:
    execute("INSERT OR IGNORE INTO mfa_state(user_id) VALUES (?)", (uid,))
    return query_one("SELECT * FROM mfa_state WHERE user_id=?", (uid,))


def _recent(text: str, minutes: int, now: datetime) -> list[str]:
    cut = _ts(now - timedelta(minutes=minutes))
    return [x for x in str(text or "").split(",") if x and x > cut]


def _code_mac(uid: int, code: str) -> str:
    from app.security.crypto import mac
    return mac("mfa-code", uid, code)


def send_code(user: dict, ip: str, force: bool = False) -> tuple[str, bool]:
    """인증번호 메일 발송. (가린 메일 주소, 새로 보냈는지) 반환.

    force=False(로그인 직후): 유효한 번호가 남아 있으면 보내지 않고 그 번호를 그대로 씀.
    force=True([다시 받기]): 새 번호를 보냄. 30초·15분 5번 제한은 어느 쪽이든 계정 기준으로 적용.
    """
    if not email_ready(user):
        raise MfaError("메일 인증을 쓸 수 없습니다. 인증 앱으로 확인하거나 관리자에게 메일 주소 등록을 요청하세요.")
    now = datetime.now()
    st = _state(user["id"])
    alive = bool(st["code_mac"]) and (st["code_exp"] or "") > _ts(now) and int(st["code_tries"] or 0) < MAX_TRIES
    if alive and not force:
        audit("auth", "인증번호 재사용(유효한 번호 있음)", user["username"], "")
        return mask(user["email"]), False
    sends = _recent(st["sends"], SEND_LIMIT[1], now)
    if len(sends) >= SEND_LIMIT[0]:
        audit("auth", "인증 메일 발송 거부(한도 초과)", user["username"], ip)
        raise MfaError(f"인증번호를 너무 자주 요청했습니다. {SEND_LIMIT[1]}분 뒤에 다시 해 주세요.")
    if force and sends and sends[-1] > _ts(now - timedelta(seconds=30)):
        raise MfaError("방금 보냈습니다. 메일함(스팸함 포함)을 확인하고, 30초 뒤에 다시 받을 수 있습니다.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    body, html = code_mail(user["display_name"], code, ip, _ts(now))
    try:
        mailer.send(user["email"], f"[{MAIL_TAG}] 로그인 인증번호 {code}", body, html=html, from_name=MAIL_TAG, plain=True)
    except mailer.MailError as ex:
        audit("auth", "로그인 인증 메일 실패", user["username"], str(ex)[:60])
        raise MfaError("인증 메일을 보내지 못했습니다. 인증 앱으로 확인하거나 관리자에게 알려 주세요.") from ex
    sends.append(_ts(now))
    execute("UPDATE mfa_state SET code_mac=?, code_exp=?, code_tries=0, sends=? WHERE user_id=?",
            (_code_mac(user["id"], code), _ts(now + timedelta(minutes=CODE_MINUTES)), ",".join(sends), user["id"]))
    audit("auth", "로그인 인증 메일 발송", user["username"], mask(user["email"]))
    return mask(user["email"]), True


def seconds_left(user: dict | None) -> int:
    """지금 유효한 인증번호의 남은 초 (없거나 만료면 0). 화면 카운트다운용."""
    if not user:
        return 0
    s = query_one("SELECT code_mac, code_exp, code_tries FROM mfa_state WHERE user_id=?", (user["id"],)) or {}
    if not s.get("code_mac") or not s.get("code_exp") or int(s.get("code_tries") or 0) >= MAX_TRIES:
        return 0
    try:
        left = (datetime.strptime(s["code_exp"], "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds()
    except ValueError:
        return 0
    return max(0, min(int(left), CODE_MINUTES * 60))


def verify_code(user: dict, code: str) -> tuple[bool, str]:
    code = re.sub(r"\D", "", code or "")
    s = _state(user["id"])
    if not s.get("code_mac") or (s.get("code_exp") or "") < _ts(datetime.now()):
        return False, "인증번호가 없거나 만료되었습니다. [인증번호 다시 받기]를 눌러 주세요."
    tries = int(s.get("code_tries") or 0)
    if tries >= MAX_TRIES:
        execute("UPDATE mfa_state SET code_mac=NULL, code_exp=NULL WHERE user_id=?", (user["id"],))
        return False, f"인증번호를 {MAX_TRIES}번 틀려 무효로 했습니다. 다시 받아 주세요."
    if len(code) != 6 or not hmac.compare_digest(_code_mac(user["id"], code), s["code_mac"]):
        execute("UPDATE mfa_state SET code_tries=code_tries+1 WHERE user_id=?", (user["id"],))
        left = MAX_TRIES - tries - 1
        return False, f"인증번호가 맞지 않습니다 (남은 시도 {left}번)" if left else "인증번호가 맞지 않습니다. 다시 받아 주세요."
    execute("UPDATE mfa_state SET code_mac=NULL, code_exp=NULL, code_tries=0 WHERE user_id=?", (user["id"],))
    return True, ""


# ── 2단계 인증 실패 횟수 (계정 기준, 비밀번호가 맞아도 지워지지 않음) ──────────
def note_fail(user: dict) -> tuple[int, bool]:
    """2단계 인증 실패 1번 기록. (최근 실패 횟수, 잠가야 하는지) 반환."""
    now = datetime.now()
    st = _state(user["id"])
    fails = _recent(st["fails"], FAIL_WINDOW_MIN, now) + [_ts(now)]
    execute("UPDATE mfa_state SET fails=? WHERE user_id=?", (",".join(fails[-20:]), user["id"]))
    return len(fails), len(fails) >= FAIL_LIMIT


def fail_count(uid: int) -> int:
    st = query_one("SELECT fails FROM mfa_state WHERE user_id=?", (uid,)) or {}
    return len(_recent(st.get("fails"), FAIL_WINDOW_MIN, datetime.now()))


def clear_fails(uid: int) -> None:
    """2단계 인증까지 성공했을 때만 부름."""
    execute("UPDATE mfa_state SET fails='', code_mac=NULL, code_exp=NULL, code_tries=0 WHERE user_id=?", (uid,))


def reset(uid: int) -> None:
    """관리자의 잠금 해제·2단계 초기화 때: 실패·번호를 지움 (발송 횟수는 남김)."""
    execute("UPDATE mfa_state SET fails='', code_mac=NULL, code_exp=NULL, code_tries=0 WHERE user_id=?", (uid,))
