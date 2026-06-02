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

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services import app_settings, gmail_monitor

_FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
_UTC = ZoneInfo("UTC")

# Which calendar to read. "primary" = the connected account's main calendar.
_CALENDAR_ID = "primary"

# Fallback business-hours / slotting config. The live values come from the
# AppSetting table (Customer Service → Availability); these mirror
# app_settings.DEFAULTS and are only used if a row is missing or unparseable.
_DEFAULT_TZ = "America/Chicago"  # Bay County FL is Central time
# Per-day open/close on a 24-hour clock (Mon-Fri 9-5); a day absent = closed.
_DEFAULT_DAY_HOURS: dict[int, tuple[int, int]] = dict.fromkeys(range(5), (9, 17))

# A busy block from the calendar: (start, end), both tz-aware UTC.
BusyInterval = tuple[datetime, datetime]


@dataclass(frozen=True)
class SchedulingConfig:
    """The tunable calendar window, resolved from AppSetting at request time."""

    tz: ZoneInfo
    day_hours: dict[int, tuple[int, int]]  # weekday (0=Mon) -> (open_hour, close_hour)
    slot_minutes: int
    lookahead_days: int
    max_slots: int


def parse_day_hours(raw: str) -> dict[int, tuple[int, int]]:
    """Parse the stored ``{"0": [9, 17], …}`` JSON into {weekday: (open, close)}.

    A day is only included if it has a valid window (0 ≤ open < close ≤ 23); junk,
    out-of-range, or open≥close entries are dropped (that day is treated as closed).
    If nothing valid remains, falls back to Mon-Fri 9-5 so the chatbot never ends
    up silently offering zero times because of a malformed value.
    """
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        data = {}

    result: dict[int, tuple[int, int]] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            try:
                day = int(key)
                open_h, close_h = int(value[0]), int(value[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if 0 <= day <= 6 and 0 <= open_h < close_h <= 23:
                result[day] = (open_h, close_h)
    return result or dict(_DEFAULT_DAY_HOURS)


def _resolve_tz(raw: str) -> ZoneInfo:
    try:
        return ZoneInfo(raw or _DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_TZ)


def load_scheduling_config(db: Session) -> SchedulingConfig:
    """Resolve the calendar window from the Customer Service → Availability values."""
    return SchedulingConfig(
        tz=_resolve_tz(app_settings.get_str(db, "cal_timezone")),
        day_hours=parse_day_hours(app_settings.get_str(db, "cal_day_hours")),
        slot_minutes=app_settings.get_int(db, "cal_slot_minutes", minimum=5, maximum=480),
        lookahead_days=app_settings.get_int(db, "cal_lookahead_days", minimum=1, maximum=60),
        max_slots=app_settings.get_int(db, "cal_max_slots", minimum=1, maximum=50),
    )


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


def get_open_slots(
    db: Session,
    max_slots: int | None = None,
    *,
    weekday: int | None = None,
    period: str | None = None,
) -> list[datetime]:
    """Return up to max_slots open slot start times (tz-aware) over the lookahead window.

    The business days, hours, slot length, look-ahead, timezone, and cap are read
    from the Customer Service → Availability config; pass max_slots to override
    the cap. Optional filters narrow to the customer's stated preference:
      - weekday: only that day of week (0=Mon … 6=Sun)
      - period: "morning" (before noon) or "afternoon" (noon onward)
    Filtering happens before the cap, so a Saturday-afternoon request returns
    Saturday-afternoon openings rather than the first N slots of the week.
    """
    cfg = load_scheduling_config(db)
    cap = cfg.max_slots if max_slots is None else max_slots
    period = period if period in ("morning", "afternoon") else None

    now = datetime.now(tz=cfg.tz)
    horizon = now + timedelta(days=cfg.lookahead_days)
    busy = _get_busy(db, now, horizon)

    slots: list[datetime] = []
    day = now.date()
    for _ in range(cfg.lookahead_days + 1):
        hours = cfg.day_hours.get(day.weekday())
        if hours and (weekday is None or day.weekday() == weekday):
            open_hour, close_hour = hours
            slot_start = datetime(day.year, day.month, day.day, open_hour, 0, tzinfo=cfg.tz)
            day_close = datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=cfg.tz)
            while slot_start < day_close:
                slot_end = slot_start + timedelta(minutes=cfg.slot_minutes)
                in_period = (
                    period is None
                    or (period == "morning" and slot_start.hour < 12)
                    or (period == "afternoon" and slot_start.hour >= 12)
                )
                if slot_start > now and in_period and not _overlaps(slot_start, slot_end, busy):
                    slots.append(slot_start)
                    if len(slots) >= cap:
                        return slots
                slot_start = slot_end
        day += timedelta(days=1)
    return slots


def slot_is_available(db: Session, start: datetime) -> bool:
    """True if ``start`` is a real, still-open consultation slot.

    Re-derives the rules from config and re-checks free/busy so a booking can be
    validated independently of whatever was shown earlier in the conversation —
    the model can't book a time we never offered, a past time, or one that filled
    up in the meantime.
    """
    cfg = load_scheduling_config(db)
    start = start.astimezone(cfg.tz)
    now = datetime.now(tz=cfg.tz)
    if start <= now or start > now + timedelta(days=cfg.lookahead_days):
        return False
    hours = cfg.day_hours.get(start.weekday())
    if not hours:
        return False
    open_hour, close_hour = hours
    day_open = start.replace(hour=open_hour, minute=0, second=0, microsecond=0)
    day_close = start.replace(hour=close_hour, minute=0, second=0, microsecond=0)
    slot_end = start + timedelta(minutes=cfg.slot_minutes)
    if start < day_open or slot_end > day_close:
        return False
    # Must land exactly on the slot grid that starts at the day's opening hour.
    offset_min = (start - day_open).total_seconds() / 60
    if offset_min % cfg.slot_minutes != 0:
        return False
    return not _overlaps(start, slot_end, _get_busy(db, start, slot_end))


def _format_label(dt_local: datetime) -> str:
    """e.g. 'Tuesday, May 27 at 10:00 AM CST'.

    The timezone suffix is derived from the slot's own tzinfo (tzname()) so it
    stays correct when the configured timezone changes — no hardcoded 'CT'.

    Built without the %-d / %-I strftime flags, which are glibc-only and raise
    ValueError on Windows (the bug that made the old Calendly output show raw
    ISO timestamps during local testing).
    """
    time_part = dt_local.strftime("%I:%M %p").lstrip("0")
    tz_abbr = dt_local.tzname() or ""
    suffix = f" {tz_abbr}" if tz_abbr else ""
    return f"{dt_local.strftime('%A, %B')} {dt_local.day} at {time_part}{suffix}"


def format_slots_for_chat(slots: list[datetime]) -> str:
    """Format open slots as a readable chat message, with the booking link if configured."""
    booking_url = settings.google_booking_url
    if not slots:
        msg = "I don't see any open consultation times in the next week."
        if booking_url:
            return f"{msg} You can still request a time here: {booking_url}"
        return f"{msg} Please email primemicromarkets@gmail.com and we'll find a time that works."

    lines = "\n".join(f"• {_format_label(s)}" for s in slots)
    body = "Here are the next available consultation times:\n" + lines
    if booking_url:
        body += f"\n\nBook your preferred time here: {booking_url}"
    else:
        body += (
            "\n\nReply with one that works and we'll confirm, or email primemicromarkets@gmail.com."
        )
    return body


def get_availability_message(
    db: Session, *, weekday: int | None = None, period: str | None = None
) -> str:
    """High-level helper for the chatbot tool: fetch open slots and format them.

    Optional weekday/period filters narrow the openings to the customer's stated
    preference. Raises on a missing Google connection or API error — the caller
    (the chatbot tool handler) catches that and falls back to the booking link.
    """
    return format_slots_for_chat(get_open_slots(db, weekday=weekday, period=period))


# ── Event creation (booking) ───────────────────────────────────────────────────

_CALENDAR_EVENTS_URL = f"https://www.googleapis.com/calendar/v3/calendars/{_CALENDAR_ID}/events"


class CalendarWriteError(RuntimeError):
    """Creating a calendar event failed. ``needs_reconnect`` is True when the cause
    is an insufficient scope (Google must be reconnected to grant calendar.events)."""

    def __init__(self, message: str, *, needs_reconnect: bool = False) -> None:
        super().__init__(message)
        self.needs_reconnect = needs_reconnect


def _is_scope_error(resp: httpx.Response) -> bool:
    """Detect the 'insufficient authentication scopes' signal from Calendar API."""
    if resp.status_code not in (401, 403):
        return False
    text = resp.text or ""
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text or "insufficientPermissions" in text:
        return True
    return "insufficient authentication scopes" in text.lower()


def create_event(
    db: Session,
    *,
    start: datetime,
    summary: str,
    description: str,
    attendee_email: str | None = None,
) -> str:
    """Create a consultation event on the company calendar; return its link.

    Uses get_valid_access_token (always fresh — auto-refreshed with a 5-minute
    margin) so the write never rides a near-expired token. Raises CalendarWriteError
    (needs_reconnect=True) if the grant lacks the calendar.events scope so the
    caller can degrade gracefully and the UI can prompt a reconnect.
    """
    cfg = load_scheduling_config(db)
    start = start.astimezone(cfg.tz)
    end = start + timedelta(minutes=cfg.slot_minutes)
    token = gmail_monitor.get_valid_access_token(db)

    body: dict = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": str(cfg.tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(cfg.tz)},
    }
    params: dict = {}
    if attendee_email:
        body["attendees"] = [{"email": attendee_email}]
        params["sendUpdates"] = "all"

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=10) as client:
        resp = client.post(_CALENDAR_EVENTS_URL, headers=headers, json=body, params=params)
        if _is_scope_error(resp):
            gmail_monitor.flag_calendar_reauth(db)
            raise CalendarWriteError(
                "Google grant is missing calendar write permission.", needs_reconnect=True
            )
        # Inviting an attendee can be rejected by account policy; retry once without
        # the invite so the event still lands on the company calendar.
        if resp.status_code >= 400 and attendee_email:
            body.pop("attendees", None)
            resp = client.post(_CALENDAR_EVENTS_URL, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json().get("htmlLink", "")


def booking_enabled(db: Session) -> bool:
    """True when the chatbot can write events (Google connected with events scope)."""
    return gmail_monitor.has_calendar_write(db)
