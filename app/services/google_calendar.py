"""Google Calendar availability via the FreeBusy API.

Replaces the old Calendly client. Reuses the Google OAuth tokens stored by
gmail_monitor (same Google app + client credentials), so no second integration
is needed — see gmail_monitor._SCOPES, which now includes calendar.freebusy.

This reads ONLY free/busy blocks on the connected account's calendar — never
event titles or details — and subtracts them from configured business hours to
compute open consultation slots. The chatbot shows those slots and links to the
Google Calendar Appointment Schedule page (settings.google_booking_url) for the
customer to confirm.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services import gmail_monitor

_FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
_UTC = ZoneInfo("UTC")
_TZ = ZoneInfo("America/Chicago")  # Bay County FL is Central time

# Which calendar to read. "primary" = the connected account's main calendar.
_CALENDAR_ID = "primary"

# Business-hours / slotting config. Constants for now; promote to AppSetting later
# if the team wants to tune these from the UI.
_BUSINESS_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri (Monday = 0)
_OPEN_HOUR = 9  # first slot starts 9:00 AM CT
_CLOSE_HOUR = 17  # last slot ends by 5:00 PM CT
_SLOT_MINUTES = 30
_LOOKAHEAD_DAYS = 7
_MAX_SLOTS = 8

# A busy block from the calendar: (start, end), both tz-aware UTC.
BusyInterval = tuple[datetime, datetime]


def _get_busy(db: Session, time_min: datetime, time_max: datetime) -> list[BusyInterval]:
    """Query the FreeBusy API and return busy intervals as (start, end) UTC datetimes."""
    token = gmail_monitor.get_valid_access_token(db)
    body = {
        "timeMin": time_min.astimezone(_UTC).isoformat(),
        "timeMax": time_max.astimezone(_UTC).isoformat(),
        "items": [{"id": _CALENDAR_ID}],
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(
            _FREEBUSY_URL,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    busy_raw = data.get("calendars", {}).get(_CALENDAR_ID, {}).get("busy", [])
    busy: list[BusyInterval] = []
    for block in busy_raw:
        try:
            start = datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
            busy.append((start, end))
        except (KeyError, ValueError):
            continue
    return busy


def _overlaps(slot_start: datetime, slot_end: datetime, busy: list[BusyInterval]) -> bool:
    return any(slot_start < b_end and slot_end > b_start for b_start, b_end in busy)


def get_open_slots(db: Session, max_slots: int = _MAX_SLOTS) -> list[datetime]:
    """Return up to max_slots open slot start times (tz-aware, Central) over the next week."""
    now = datetime.now(tz=_TZ)
    horizon = now + timedelta(days=_LOOKAHEAD_DAYS)
    busy = _get_busy(db, now, horizon)

    slots: list[datetime] = []
    day = now.date()
    for _ in range(_LOOKAHEAD_DAYS + 1):
        if day.weekday() in _BUSINESS_DAYS:
            slot_start = datetime(day.year, day.month, day.day, _OPEN_HOUR, 0, tzinfo=_TZ)
            day_close = datetime(day.year, day.month, day.day, _CLOSE_HOUR, 0, tzinfo=_TZ)
            while slot_start < day_close:
                slot_end = slot_start + timedelta(minutes=_SLOT_MINUTES)
                if slot_start > now and not _overlaps(slot_start, slot_end, busy):
                    slots.append(slot_start)
                    if len(slots) >= max_slots:
                        return slots
                slot_start = slot_end
        day += timedelta(days=1)
    return slots


def _format_label(dt_local: datetime) -> str:
    """e.g. 'Tuesday, May 27 at 10:00 AM CT'.

    Built without the %-d / %-I strftime flags, which are glibc-only and raise
    ValueError on Windows (the bug that made the old Calendly output show raw
    ISO timestamps during local testing).
    """
    time_part = dt_local.strftime("%I:%M %p").lstrip("0")
    return f"{dt_local.strftime('%A, %B')} {dt_local.day} at {time_part} CT"


def format_slots_for_chat(slots: list[datetime]) -> str:
    """Format open slots as a readable chat message, with the booking link if configured."""
    booking_url = settings.google_booking_url
    if not slots:
        msg = "I don't see any open consultation times in the next week."
        if booking_url:
            return f"{msg} You can still request a time here: {booking_url}"
        return f"{msg} Please email primemicromarkets@gmail.com and we'll find a time that works."

    lines = "\n".join(f"• {_format_label(s)}" for s in slots)
    body = "Here are the next available consultation times (Central):\n" + lines
    if booking_url:
        body += f"\n\nBook your preferred time here: {booking_url}"
    else:
        body += (
            "\n\nReply with one that works and we'll confirm, or email primemicromarkets@gmail.com."
        )
    return body


def get_availability_message(db: Session) -> str:
    """High-level helper for the chatbot tool: fetch open slots and format them.

    Raises on a missing Google connection or API error — the caller (the chatbot
    tool handler) catches that and falls back to the booking link / email.
    """
    return format_slots_for_chat(get_open_slots(db))
