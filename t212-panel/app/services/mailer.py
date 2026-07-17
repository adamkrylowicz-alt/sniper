"""
app/services/mailer.py
========================
Cienki wrapper na smtplib - wysyłka emaili (na razie tylko: zgłoszenia
"Zgłoś problem"). Skonfigurowany pod Gmail SMTP w config.py, ale powinien
działać z każdym dostawcą wspierającym STARTTLS na porcie 587.
"""

from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage


class MailerNotConfiguredError(Exception):
    """Podniesione gdy brakuje SMTP_USER/SMTP_PASSWORD/REPORT_TO_EMAIL w .env."""


class MailSendError(Exception):
    """Podniesione gdy SMTP odrzuci połączenie/wysyłkę (złe hasło, blokada itp.)."""


def send_report(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str | None,
    smtp_password: str | None,
    mail_from: str | None,
    mail_to: str | None,
    subject: str,
    body_text: str,
    attachment_filename: str | None = None,
    attachment_bytes: bytes | None = None,
) -> None:
    if not smtp_user or not smtp_password or not mail_to:
        raise MailerNotConfiguredError(
            "Brak konfiguracji SMTP w .env - potrzebne SMTP_USER, "
            "SMTP_PASSWORD (App Password Gmaila, NIE zwykłe hasło) i "
            "REPORT_TO_EMAIL. Patrz komentarz w config.py."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from or smtp_user
    msg["To"] = mail_to
    msg.set_content(body_text)

    if attachment_filename and attachment_bytes:
        mime_type, _ = mimetypes.guess_type(attachment_filename)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        msg.add_attachment(
            attachment_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=attachment_filename,
        )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailSendError(f"Wysyłka nie powiodła się: {exc}") from exc
