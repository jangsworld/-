"""메일 발송 (전자서명 요청·증명서 전송).

메일플러그 등 SMTP 계정은 설정 화면에서 입력하며 앱 비밀번호는 DB 에 암호화 저장합니다.
화면 설정이 없으면 bms.toml [mail] 을 사용합니다. 이때 비밀번호는 동기화 폴더에 놓이는 bms.toml 에 넣지 않고
환경변수 BMS_SMTP_PASSWORD 또는 {secrets_dir}/smtp_password.txt 에서 읽습니다.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.config import settings
from app.db import get_settings


class MailError(Exception):
    pass


def config() -> dict:
    s = get_settings()
    if s.get("mail_host") and s.get("mail_user"):
        port = int(s.get("mail_port") or 465)
        return {"host": s["mail_host"], "port": port, "ssl": port == 465, "user": s["mail_user"],
                "password": s.get("mail_password") or "", "from": s.get("mail_from") or s["mail_user"],
                "name": s.get("mail_from_name") or "", "cc": s.get("mail_cc") or "",
                "reply_to": s.get("mail_reply_to") or "", "signature": s.get("mail_signature") or ""}
    return {"host": settings.smtp_host, "port": settings.smtp_port, "ssl": settings.smtp_ssl, "user": settings.smtp_user,
            "password": _password(), "from": settings.smtp_from or settings.smtp_user, "name": "", "cc": "", "reply_to": "",
            "signature": ""}


def configured() -> bool:
    c = config()
    return bool(c["host"] and c["from"])


def _password() -> str:
    env = os.environ.get("BMS_SMTP_PASSWORD")
    if env:
        return env
    path = os.path.join(settings.secrets_dir, "smtp_password.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def send(to: str, subject: str, body: str, attachments: list[tuple[str, bytes]] | None = None):
    c = config()
    if not c["host"] or not c["from"]:
        raise MailError("메일 서버가 설정되지 않았습니다")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((c["name"], c["from"]))
    msg["To"] = to
    if c["cc"]:
        msg["Cc"] = c["cc"]
    if c["reply_to"]:
        msg["Reply-To"] = c["reply_to"]
    msg.set_content(f"{body}\n\n{c['signature']}" if c["signature"] else body)
    for name, data in attachments or []:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    ctx = ssl.create_default_context()
    try:
        if c["ssl"]:
            server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(c["host"], c["port"], timeout=20)
            server.starttls(context=ctx)   # 개인정보가 담기므로 암호화 없는 발송은 허용하지 않음
        with server:
            if c["user"]:
                server.login(c["user"], c["password"])
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as ex:
        raise MailError("메일 로그인 실패: 계정과 앱 비밀번호를 확인하세요 (앱 비밀번호는 2주 미사용 시 삭제됨)") from ex
    except (smtplib.SMTPException, OSError) as ex:
        raise MailError(f"메일 발송 실패: {ex.__class__.__name__}") from ex
