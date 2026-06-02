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
# AppSetting table (Settings → Scheduling); these mirror app_settings.DEFAULTS
# and are only used if a row is missing or unparseable.
_DEFAULT_TZ = "America/Chicago"  # Bay County FL is Central time
_DEFAULT_BUSINESS_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri (Monday = 0)

# A busy block from the calendar: (start, end), both tz-aware UTC.
BusyInterval = tuple[datetime, datetime]


@dataclass(frozen=True)
class SchedulingConfig:
    """The tunable calendar window, resolved from AppSetting at request time."""

    tz: ZoneInfo
    business_days: set[int]
    open_hour: int
    close_hour: int
    slot_minutes: int
    lookahead_days: int
    max_slots: int


def parse_business_days(raw: str) -> set[int]:
    """Parse a stored ``"0,1,2,3,4"`` string into a set of weekday ints (0=Mon).

    Ignores junk and out-of-range values; falls back to Mon-Fri if nothing valid
    remains so the chatbot never ends up offering zero days by accident.
    """
    days: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.add(int(part))
    return days or set(_DEFAULT_BUSINESS_DAYS)


def _resolve_tz(raw: str) -> ZoneInfo:
    try:
        return ZoneInfo(raw or _DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_TZ)


def load_scheduling_config(db: Session) -> SchedulingConfig:
    """Resolve the calendar window from the Settings → Scheduling values."""
    open_hour = app_settings.get_int(db, "cal_open_hour", minimum=0, maximum=23)
    close_hour = app_settings.get_int(db, "cal_close_hour", minimum=1, maximum=23)
    # A close hour at or below open would yield no slots; widen to a safe default.
    if close_hour <= open_hour:
        close_hour = max(open_hour + 1, 17)
    return SchedulingConfig(
        tz=_resolve_tz(app_settings.get_str(db, "cal_timezone")),
        business_days=parse_business_days(app_settings.get_str(db, "cal_business_days")),
        open_hour=open_hour,
        close_hour=close_hour,
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


def get_open_slots(db: Session, max_slots: int | None = None) -> list[datetime]:
    """Return up to max_slots open slot start times (tz-aware) over the lookahead window.

    The business days, hours, slot length, look-ahead, timezone, and cap are read
    from the Settings → Scheduling config; pass max_slots to override the cap.
    """
    cfg = load_scheduling_config(db)
    cap = cfg.max_slots if max_slots is None else max_slots

    now = datetime.now(tz=cfg.tz)
    horizon = now + timedelta(days=cfg.lookahead_days)
    busy = _get_busy(db, now, horizon)

    slots: list[datetime] = []
    day = now.date()
    for _ in range(cfg.lookahead_days + 1):
        if day.weekday() in cfg.business_days:
            slot_start = datetime(day.year, day.month, day.day, cfg.open_hour, 0, tzinfo=cfg.tz)
            day_close = datetime(day.year, day.month, day.day, cfg.close_hour, 0, tzinfo=cfg.tz)
            while slot_start < day_close:
                slot_end = slot_start + timedelta(minutes=cfg.slot_minutes)
                if slot_start > now and not _overlaps(slot_start, slot_end, busy):
                    slots.append(slot_start)
                    if len(slots) >= cap:
                        return slots
                slot_start = slot_end
        day += timedelta(days=1)
    return slots


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
            "\n\nReply with one that works and we'll confirm, "
            "or email primemicromarkets@gmail.com."
        )
    return body


def get_availability_message(db: Session) -> str:
    """High-level helper for the chatbot tool: fetch open slots and format them.

    Raises on a missing Google connection or API error — the caller (the chatbot
    tool handler) catches that and falls back to the booking link / email.
    """
    return format_slots_for_chat(get_open_slots(db))
