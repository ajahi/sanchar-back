"""Outgoing mail over SMTP (stdlib). Without MAIL_HOST it logs the message instead (dev)."""
import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

log = logging.getLogger(__name__)


def _send(msg: EmailMessage) -> None:
    if settings.mail_port == 465:
        smtp = smtplib.SMTP_SSL(settings.mail_host, 465, timeout=15)
    else:
        smtp = smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=15)
        smtp.starttls()
    with smtp:
        smtp.login(settings.mail_username, settings.mail_password)
        smtp.send_message(msg)


async def send_email(to: str, subject: str, body: str) -> None:
    """Best-effort: SMTP failures are logged, never raised (signup must not 500 on mail)."""
    if not settings.mail_host:
        log.info("MAIL (dev, not sent) to=%s subject=%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = f"{settings.mail_from_name} <{settings.mail_from_address}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        await asyncio.to_thread(_send, msg)
    except Exception:
        log.exception("failed to send email to %s", to)


async def send_verification_email(to: str, name: str, token: str) -> None:
    link = f"{settings.frontend_url.rstrip('/')}/verify?token={token}"
    await send_email(
        to,
        "Confirm your Sanchar account",
        f"Hi {name},\n\nConfirm your email to activate your Sanchar account:\n\n{link}\n\n"
        f"This link expires in {settings.email_verify_expire_hours} hours. "
        "If you didn't sign up, ignore this email.\n",
    )
