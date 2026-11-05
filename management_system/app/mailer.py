"""메일 발송 (전자서명 요청·증명서 전송).

SMTP 비밀번호는 동기화 폴더에 놓이는 bms.toml 에 넣지 않고
환경변수 BMS_SMTP_PASSWORD 또는 {secrets_dir}/smtp_password.txt 에서 읽습니다.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

from app.config import settings


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))


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
    if not configured():
        raise MailError("메일 서버가 설정되지 않았습니다")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg.set_content(body)
    for name, data in attachments or []:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    ctx = ssl.create_default_context()
    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            server.starttls(context=ctx)   # 개인정보가 담기므로 암호화 없는 발송은 허용하지 않음
        with server:
            if settings.smtp_user:
                server.login(settings.smtp_user, _password())
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as ex:
        raise MailError(f"메일 발송 실패: {ex.__class__.__name__}") from ex
