"""Email sending: Gmail API (preferred) with an SMTP fallback.

Two delivery paths:

  * ``send_via_gmail_api`` posts the message through the Gmail REST API over
    HTTPS (port 443) using the connected Google OAuth grant (gmail.send scope).
    This is the preferred path in production because some hosts (Render among
    them) block outbound SMTP ports — an SMTP attempt there hangs until it times
    out, which both slows the request and fails to deliver.
  * ``send_email`` is the classic Gmail SMTP path (STARTTLS, port 587) using an
    app password. Kept as a fallback for environments where SMTP is open and as
    the path used when Google isn't OAuth-connected.

``send_appointment_confirmation`` tries the API first and falls back to SMTP.
"""

from __future__ import annotations

import base64
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from sqlalchemy.orm import Session

from app.config import settings

_log = logging.getLogger(__name__)

_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _build_message(
    to_address: str, subject: str, body: str, from_address: str | None
) -> MIMEMultipart:
    msg = MIMEMultipart()
    if from_address:
        msg["From"] = from_address
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    return msg


def send_email(to_address: str, subject: str, body: str) -> None:
    """Send a plain-text email via Gmail SMTP (STARTTLS, port 587)."""
    if not settings.gmail_user or not settings.gmail_app_password:
        raise RuntimeError(
            "GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env. "
            "Generate an App Password at myaccount.google.com/apppasswords."
        )

    msg = _build_message(to_address, subject, body, settings.gmail_user)

    # timeout guards against an indefinite hang if outbound SMTP is filtered.
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.gmail_user, settings.gmail_app_password)
        server.send_message(msg)


def send_via_gmail_api(db: Session, to_address: str, subject: str, body: str) -> None:
    """Send a plain-text email through the Gmail REST API (HTTPS).

    Uses the connected Google OAuth access token (gmail.send scope). Raises if
    Google isn't connected (no refresh token) or the API rejects the request, so
    callers can fall back to SMTP.
    """
    # Imported lazily to avoid a circular import (gmail_monitor → app_settings → …).
    from app.services import gmail_monitor

    token = gmail_monitor.get_valid_access_token(db)
    msg = _build_message(to_address, subject, body, settings.gmail_user or None)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            _GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
        )
        resp.raise_for_status()


def send_appointment_confirmation(db: Session, to_address: str, subject: str, body: str) -> None:
    """Deliver a message, preferring the Gmail API and falling back to SMTP.

    The API path works where outbound SMTP is blocked (e.g. Render), so it's tried
    first whenever Google is connected; any failure (not connected, scope/API
    error) falls through to the SMTP app-password path. Raises only if BOTH fail.
    """
    try:
        send_via_gmail_api(db, to_address, subject, body)
        return
    except Exception as exc:
        _log.warning("Gmail API send failed (%s); falling back to SMTP.", exc)
    send_email(to_address, subject, body)
