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
from datetime import datetime, timedelta, timezone
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


MAIL_COLOR = "#1f3a5f"     # 화면 왼쪽 메뉴와 같은 색 (TBM·공문 인증 메일과 같은 모양, 2026-11)
LOGO_CID = "bmslogo"
KST = timezone(timedelta(hours=9))
_LAN_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")     # 사내망으로 보는 주소 범위
_UA_OK = re.compile(r"^Mozilla/5\.0[A-Za-z0-9.,;:_/()+\- ]*$")
_UA_LINKY = re.compile(r"[A-Za-z]\.[A-Za-z]|//|:/|www\.", re.I)     # 메일 프로그램이 링크로 바꿀 수 있는 모양


def device_label(ua: str) -> str:
    """브라우저가 보낸 긴 문자열을 '브라우저 버전 · 기기' 로 줄임 (예: Chrome 157 · Windows)."""
    u = str(ua or "")[:400]
    names = (("Edg", "Edge"), ("Whale", "네이버 웨일"), ("SamsungBrowser", "삼성 인터넷"), ("OPR", "Opera"),
             ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Version", "Safari"))
    systems = (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
               ("Mac OS X", "Mac"), ("Linux", "Linux"))
    br = ""
    for key, label in names:
        m = re.search(key + r"[A-Za-z]*/(\d{1,4})", u)
        if m and (key != "Version" or "Safari/" in u):
            br = f"{label} {m.group(1)}"
            break
    os_ = next((label for key, label in systems if key in u), "")
    return " · ".join(x for x in (br, os_) if x) or "알 수 없음"


def browser_text(ua: str) -> str:
    """브라우저 원문. 흔한 형식일 때만 그대로 보여 주고, 링크로 바뀔 수 있는 글자가 있거나 낯선 형식이면 보여 주지 않음."""
    u = str(ua or "").strip()
    if not u or len(u) > 400 or not _UA_OK.match(u) or _UA_LINKY.search(u):
        return "표시하지 않음"
    return u


def ip_label(ip: str) -> str:
    """IP 주소 뒤에 (사내) / (전용망) / (외부) 를 붙임."""
    import ipaddress
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return _clip(ip, 45)
    if a in ipaddress.ip_network("100.64.0.0/10"):
        kind = "전용망"
    elif (a.is_loopback or a.is_link_local or (a.version == 4 and any(a in ipaddress.ip_network(n) for n in _LAN_NETS))
          or (a.version == 6 and a in ipaddress.ip_network("fc00::/7"))):
        kind = "사내"
    else:
        kind = "외부"
    return f"{a} ({kind})"


def _clip(v, n: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()[:n]


def code_mail(name: str, code: str, ip: str, at: str, username: str = "", ua: str = "",
              org: str = "", logo: bool = False) -> tuple[str, str]:
    """인증번호 메일 본문 (일반 텍스트, HTML). 제목에 번호가 있어 폰 알림에서 바로 보임.

    모양(TBM·공문과 같음): 남색 띠 → 로고(메일에 넣은 그림, 없으면 회사 이름) → 시스템 이름 → 제목 → 어느 계정인지
    → 큰 번호 → 3분·한 번 → 로그인 시도 정보(시간·IP 주소·기기·브라우저) → 빨간 안내 → 이 메일을 받는 이유.
    누르는 링크와 바깥 그림은 넣지 않음. 표와 인라인 스타일만 씀(메일 프로그램 호환)."""
    name, username, at, org = _clip(name, 40), _clip(username, 40), _clip(at, 25), _clip(org, 40)
    who = f"{name}님({username})" if username and username != name else f"{name}님"
    ip_s, device, browser = ip_label(ip), device_label(ua), browser_text(ua)
    title = "로그인 시도에 인증이 필요합니다"
    lead = f"{who} 계정으로 새 로그인 시도가 있습니다."
    warn = (f"본인이 로그인한 것이 아니면 이 번호를 누구에게도 알려 주지 말고, {MAIL_TAG}에 직접 접속해 "
            "비밀번호를 바꾼 뒤 관리자에게 알려 주세요.")
    why = (f"이 메일은 {MAIL_TAG}의 2단계 인증을 위해 자동으로 보낸 것입니다. 관리자를 포함해 누구도 인증번호를 묻지 않습니다. "
           "이 메일에는 누르는 링크가 없습니다.")
    foot = " · ".join(x for x in (org, MAIL_TAG) if x)
    text = (f"{MAIL_TAG} — {title}\n\n{lead}\n\n인증번호\n\n    {code}\n\n{CODE_MINUTES}분 안에 입력해 주세요. 한 번만 쓸 수 있습니다.\n\n"
            f"로그인 시도 정보\n- 시간: {at} (한국 시간)\n- IP 주소: {ip_s}\n- 기기: {device}\n- 브라우저: {browser}\n\n"
            f"{warn}\n\n{why}" + (f"\n\n{org}" if org else ""))
    e = html_escape
    f = "font-family:'Malgun Gothic','맑은 고딕','Apple SD Gothic Neo',Arial,sans-serif;word-break:keep-all;overflow-wrap:anywhere;"
    head = (f'<img src="cid:{LOGO_CID}" width="180" alt="{e(org or MAIL_TAG)}" style="display:block;width:180px;height:auto;border:0">'
            if logo else f'<p style="margin:0;font-size:20px;font-weight:bold;color:{MAIL_COLOR};{f}">{e(org or MAIL_TAG)}</p>')

    def row(label: str, value: str) -> str:
        return (f'<tr><td width="90" style="padding:8px 0;border-bottom:1px solid #dee2e6;font-size:14px;color:#6c757d;{f}">{label}</td>'
                f'<td style="padding:8px 0;border-bottom:1px solid #dee2e6;font-size:14px;color:#212529;{f}">{e(value)}</td></tr>')

    page = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"></head>'
        f'<body style="margin:0;padding:0;background:#f1f3f5;{f}color:#212529">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f3f5"><tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="width:100%;max-width:560px;background:#ffffff">'
        f'<tr><td height="8" bgcolor="{MAIL_COLOR}" style="height:8px;line-height:8px;font-size:0;background:{MAIL_COLOR}">&nbsp;</td></tr>'
        f'<tr><td style="padding:28px 24px 0">{head}'
        f'<p style="margin:10px 0 0;font-size:14px;color:#6c757d;{f}">{MAIL_TAG}</p></td></tr>'
        f'<tr><td style="padding:22px 24px 0"><h1 style="margin:0 0 12px;font-size:22px;line-height:1.4;color:#212529;{f}">{title}</h1>'
        f'<p style="margin:0;font-size:15px;line-height:1.7;{f}">{e(lead)}<br>아래 번호를 화면에 입력해 주세요.</p></td></tr>'
        '<tr><td align="center" style="padding:22px 24px 6px">'
        f'<div style="background:#eef3f9;padding:18px 8px;font-size:36px;font-weight:bold;letter-spacing:8px;color:{MAIL_COLOR};'
        f'font-family:Consolas,\'Courier New\',monospace">{e(code)}</div>'
        f'<p style="margin:10px 0 0;font-size:14px;color:#495057;{f}"><b>{CODE_MINUTES}분 안에</b> 입력해 주세요. 한 번만 쓸 수 있습니다.</p></td></tr>'
        f'<tr><td style="padding:22px 24px 0"><h2 style="margin:0 0 8px;font-size:16px;color:#212529;{f}">로그인 시도 정보</h2>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #dee2e6">'
        + row("시간", f"{at} (한국 시간)") + row("IP 주소", ip_s) + row("기기", device) + row("브라우저", browser) + '</table></td></tr>'
        f'<tr><td style="padding:18px 24px 0"><p style="margin:0;font-size:14px;line-height:1.7;color:#c0392b;{f}">{e(warn)}</p></td></tr>'
        f'<tr><td style="padding:20px 24px 28px"><h2 style="margin:0 0 6px;font-size:14px;color:#495057;{f}">이 메일을 받는 이유</h2>'
        f'<p style="margin:0;font-size:13px;line-height:1.7;color:#6c757d;{f}">{e(why)}</p></td></tr>'
        f'</table><p style="margin:14px 0 0;font-size:12px;color:#868e96;{f}">{e(foot)}</p>'
        '</td></tr></table></body></html>')
    return text, page


def _mail_brand() -> tuple[str, list]:
    """(회사 이름, 메일에 첨부할 로고). 로고·이름은 환경설정에 올린 것(본사)이며 로고는 여백을 잘라 작게 줄임. 없으면 빈 값."""
    try:
        from app import brand
        got = brand.mail_logo()
        org = str(get_settings().get("company_name") or "")
    except Exception:
        return "", []
    return org, ([(LOGO_CID, got[0], got[1])] if got and len(got[0]) <= 400_000 else [])


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


def send_code(user: dict, ip: str, force: bool = False, ua: str = "") -> tuple[str, bool]:
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
    org, logo = _mail_brand()
    body, html = code_mail(user["display_name"], code, ip, _ts(datetime.now(KST)), username=user["username"], ua=ua,
                           org=org, logo=bool(logo))
    try:
        mailer.send(user["email"], f"[{MAIL_TAG}] 로그인 인증번호 {code}", body, html=html, from_name=MAIL_TAG, plain=True,
                    inline=logo)
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
