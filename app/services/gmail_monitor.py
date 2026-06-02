"""Gmail API integration for monitoring the company inbox and drafting AI replies.

OAuth flow: Google OAuth 2.0 with gmail.readonly + gmail.send + calendar.freebusy +
calendar.events scopes.
Tokens are stored in AppSetting (gmail_refresh_token, gmail_access_token, gmail_token_expiry).
The existing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are reused.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.email_approval import EmailApproval
from app.models.settings import AppSetting
from app.services import cs_email_agent

_log = logging.getLogger(__name__)


class GmailReauthRequiredError(RuntimeError):
    """Refresh token is dead (revoked, expired, or scope-mismatched).

    Raised after the stored token rows have been cleared, so the next
    poll iteration sees ``gmail_connected = False`` and skips silently
    until an operator re-consents at ``/customer-service/gmail/connect``.
    """


# Errors from Google's token endpoint that mean the refresh token is gone for
# good. Listed in https://datatracker.ietf.org/doc/html/rfc6749#section-5.2 —
# the only practical recovery is a fresh consent.
_DEAD_GRANT_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})

# Look back this far on the very first poll (no high-water mark stored yet).
_INITIAL_LOOKBACK_SECONDS = 7 * 24 * 3600
# Re-scan this much before the last high-water mark each poll, so a message that
# arrived right at the previous boundary is never skipped. Dedup makes the
# overlap harmless.
_OVERLAP_SECONDS = 3600
_MAX_RESULTS = 50

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # Read-only free/busy for the chatbot's live availability tool, plus events
    # write so the chatbot can book confirmed consultations on the calendar
    # (app/services/google_calendar.py). Adding/changing a scope means the stored
    # refresh token must be re-minted: re-run /customer-service/gmail/connect once
    # after deploy to re-consent.
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
]


# ── Settings helpers ──────────────────────────────────────────────────────────


def _get(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else ""


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


# ── OAuth helpers ─────────────────────────────────────────────────────────────


def build_gmail_auth_url(redirect_uri: str, state: str) -> tuple[str, str]:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}", state


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        return resp.json()


def store_tokens(db: Session, token_data: dict) -> None:
    _set(db, "gmail_access_token", token_data.get("access_token", ""))
    refresh = token_data.get("refresh_token", "")
    if refresh:
        _set(db, "gmail_refresh_token", refresh)
    # Persist the scopes Google actually granted so the UI can tell whether calendar
    # booking is enabled without making an API call. Google omits "scope" on a plain
    # refresh when nothing changed, so only overwrite when it's actually present.
    scope = token_data.get("scope", "")
    if scope:
        _set(db, "gmail_granted_scopes", scope)
    expires_in = int(token_data.get("expires_in", 3600))
    expiry = (datetime.now(tz=UTC) + timedelta(seconds=expires_in)).isoformat()
    _set(db, "gmail_token_expiry", expiry)
    # A fresh consent clears any pending reauth banner — Gmail and calendar both.
    _set(db, "gmail_reauth_required", "")
    _set(db, "gmail_reauth_at", "")
    _set(db, "calendar_reauth_required", "")
    db.commit()


def _clear_stored_tokens(db: Session, reason: str) -> None:
    """Wipe dead OAuth state and flag the UI to prompt a reconnect.

    Called when Google rejects the refresh token (revoked, expired past 6
    months unused, or scope-mismatched). After this runs, ``_gmail_connected``
    returns False so the poll loop short-circuits, and the UI surfaces a
    "Reconnect Gmail" banner via the ``gmail_reauth_required`` flag.
    """
    for key in ("gmail_refresh_token", "gmail_access_token", "gmail_token_expiry"):
        _set(db, key, "")
    _set(db, "gmail_reauth_required", reason or "Gmail token rejected by Google")
    _set(db, "gmail_reauth_at", datetime.now(tz=UTC).isoformat())
    db.commit()


def get_valid_access_token(db: Session) -> str:
    refresh_token = _get(db, "gmail_refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "Gmail not connected. Visit /customer-service/gmail/connect to authorize."
        )

    # Check if current token is still valid
    expiry_str = _get(db, "gmail_token_expiry")
    access_token = _get(db, "gmail_access_token")
    if access_token and expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if datetime.now(tz=UTC) < expiry - timedelta(minutes=5):
                return access_token
        except Exception:
            pass

    # Refresh the token
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code in (400, 401):
            # Google encodes the dead-token signal as a JSON error field, not the
            # HTTP status alone. Parse it so we only nuke the stored token when
            # Google says the grant itself is gone (vs. a transient network/5xx
            # that should just retry on the next poll).
            try:
                err = (resp.json() or {}).get("error", "")
            except ValueError:
                err = ""
            if err in _DEAD_GRANT_ERRORS:
                reason = f"Google rejected the refresh token ({err})."
                _clear_stored_tokens(db, reason)
                raise GmailReauthRequiredError(reason)
        resp.raise_for_status()
        token_data = resp.json()

    store_tokens(db, token_data)
    return token_data["access_token"]


# ── Calendar-write scope helpers ───────────────────────────────────────────────

_CALENDAR_WRITE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar",
)


def has_calendar_write(db: Session) -> bool:
    """True if the stored grant includes a scope that allows creating events.

    Reads the persisted granted-scope string (no API call). Returns False when
    Gmail was connected before the calendar.events scope was added, signalling a
    one-time reconnect is needed to enable chatbot booking.
    """
    if not _get(db, "gmail_refresh_token"):
        return False
    granted = _get(db, "gmail_granted_scopes")
    if not granted:
        # Connected before we tracked scopes — can't confirm write access; treat as
        # not-enabled so the UI prompts a reconnect rather than silently 403-ing.
        return False
    return any(scope in granted for scope in _CALENDAR_WRITE_SCOPES)


def flag_calendar_reauth(db: Session, reason: str = "") -> None:
    """Flag that calendar booking needs a reconnect, without touching Gmail tokens.

    Used when an event write is rejected for insufficient scope. Email polling
    keeps working on the existing grant; only the booking feature is paused until
    an operator re-consents at /customer-service/gmail/connect.
    """
    _set(
        db,
        "calendar_reauth_required",
        reason or "Reconnect Google to enable chatbot calendar booking.",
    )
    db.commit()


# ── Gmail API calls ───────────────────────────────────────────────────────────


def _gmail_get(path: str, token: str, params: dict | None = None) -> dict:
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{_GMAIL_BASE}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


def _gmail_post(path: str, token: str, body: dict) -> dict:
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{_GMAIL_BASE}/{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        return decoded.strip()

    # Recurse into parts
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    # Fallback: decode HTML part if no plain text
    if mime_type == "text/html" and body_data:
        html = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        # Strip tags roughly
        return re.sub(r"<[^>]+>", " ", html).strip()

    return ""


def _extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _poll_window_query(db: Session) -> str:
    """Build the Gmail search query for new mail since the last high-water mark.

    Time-windowed (``after:`` epoch seconds) rather than ``is:unread`` so polling
    is read-state-independent: we never mutate the live inbox, and dedup by
    message id makes the overlapping rescan idempotent.
    """
    last_epoch = _get(db, "gmail_last_poll_epoch")
    now = int(time.time())
    if last_epoch:
        try:
            since = int(last_epoch) - _OVERLAP_SECONDS
        except ValueError:
            since = now - _INITIAL_LOOKBACK_SECONDS
    else:
        since = now - _INITIAL_LOOKBACK_SECONDS
    return f"category:primary -from:me after:{since}"


def poll_new_emails(db: Session) -> list[EmailApproval]:
    """Fetch recent inbound mail, classify it, and create EmailApproval records.

    Non-destructive: it does NOT mark anything read in Gmail. Customer mail is
    auto-drafted; everything else is filed with its category so the UI can show
    why it was filtered. Updates the poll high-water mark on completion.
    """
    token = get_valid_access_token(db)
    result = _gmail_get(
        "messages",
        token,
        params={"q": _poll_window_query(db), "maxResults": str(_MAX_RESULTS)},
    )
    messages = result.get("messages", [])
    created: list[EmailApproval] = []
    poll_started = int(time.time())

    for msg_ref in messages:
        msg_id = msg_ref["id"]

        # Skip if already processed
        existing = db.query(EmailApproval).filter(EmailApproval.gmail_message_id == msg_id).first()
        if existing:
            continue

        try:
            msg = _gmail_get(f"messages/{msg_id}", token, params={"format": "full"})
        except Exception:
            continue

        headers = msg.get("payload", {}).get("headers", [])
        sender_raw = _extract_header(headers, "From")
        subject = _extract_header(headers, "Subject") or "(no subject)"
        thread_id = msg.get("threadId", msg_id)
        body = _extract_body(msg.get("payload", {}))

        # Parse sender name and email
        sender_name = ""
        sender_email = sender_raw
        m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', sender_raw)
        if m:
            sender_name = m.group(1).strip()
            sender_email = m.group(2).strip()

        category, reason = cs_email_agent.classify_email(subject, body or "", sender_email, db)

        approval = EmailApproval(
            gmail_thread_id=thread_id,
            gmail_message_id=msg_id,
            sender_email=sender_email,
            sender_name=sender_name,
            original_subject=subject,
            original_body=body or "(empty body)",
            category=category,
            classification_reason=reason,
            status="pending",
        )
        try:
            db.add(approval)
            db.flush()  # catch UNIQUE violation before commit
        except IntegrityError:
            db.rollback()
            continue

        db.commit()
        db.refresh(approval)

        # Auto-draft a reply for genuine customer mail only.
        if category == "customer":
            try:
                cs_email_agent.draft_reply(approval, db)
            except Exception as exc:  # noqa: BLE001 — a draft failure must not abort the poll
                _log.warning("Auto-draft failed for approval %s: %s", approval.id, exc)
                db.rollback()

        created.append(approval)

    # Advance the high-water mark to the moment the poll began.
    _set(db, "gmail_last_poll_epoch", str(poll_started))
    _set(db, "gmail_last_poll_at", datetime.now(tz=UTC).isoformat())
    db.commit()

    return created


def send_reply_via_gmail_api(approval: EmailApproval, db: Session) -> None:
    """Send an approved email reply using the Gmail API (sends FROM the company inbox)."""
    token = get_valid_access_token(db)

    msg = MIMEMultipart()
    msg["To"] = approval.sender_email
    msg["Subject"] = approval.draft_subject or f"Re: {approval.original_subject}"
    msg["In-Reply-To"] = approval.gmail_message_id
    msg["References"] = approval.gmail_message_id
    msg.attach(MIMEText(approval.draft_body or "", "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    _gmail_post("messages/send", token, {"raw": raw, "threadId": approval.gmail_thread_id})


def poll_and_process(db: Session | None = None) -> list[EmailApproval]:
    """Entry point for the scheduler and the manual 'Poll now' button.

    Fetches recent mail, classifies it, and auto-drafts customer replies. Opens
    its own DB session when called without one (e.g. from the background loop).
    """
    if db is not None:
        return poll_new_emails(db)

    from app.database import engine

    with Session(engine) as owned_db:
        return poll_new_emails(owned_db)
