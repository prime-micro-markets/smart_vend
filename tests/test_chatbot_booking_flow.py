"""Chatbot guided booking: preference filtering, slot matching, and book_appointment.

Calendar HTTP is stubbed; these cover the agent-side logic that turns a chosen
time + contact details into a validated booking and a sales-pipeline record.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.sales import Prospect
from app.services import cs_chatbot_agent as agent
from app.services import google_calendar as gcal


def _future(db: Session, weekday_offset_hour: int = 10):
    """A real open slot datetime on the next available weekday at the given hour."""
    cfg = gcal.load_scheduling_config(db)
    d = datetime.now(cfg.tz) + timedelta(days=1)
    while d.weekday() not in cfg.day_hours:
        d += timedelta(days=1)
    return d.replace(hour=weekday_offset_hour, minute=0, second=0, microsecond=0)


# ── small parsers ──────────────────────────────────────────────────────────────


def test_parse_clock_variants() -> None:
    assert agent._parse_clock("10:00 AM") == (10, 0)
    assert agent._parse_clock("2pm") == (14, 0)
    assert agent._parse_clock("14:30") == (14, 30)
    assert agent._parse_clock("12:00 AM") == (0, 0)
    assert agent._parse_clock("12 PM") == (12, 0)
    assert agent._parse_clock("nope") is None


def test_parse_month_day_and_weekday() -> None:
    assert agent._parse_month_day("June 6") == (6, 6)
    assert agent._parse_month_day("2026-06-06") == (6, 6)
    assert agent._parse_month_day("6/6") == (6, 6)
    assert agent._parse_weekday("this Saturday") == 5
    assert agent._parse_weekday("no day here") is None


# ── slot matching ──────────────────────────────────────────────────────────────


def test_match_open_slot_by_time_and_day(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    slot = _future(db, 10)
    label_date = slot.strftime("%B %d")  # e.g. "June 06"
    matched = agent._match_open_slot(db, label_date, "10:00 AM")
    assert matched is not None
    assert (matched.month, matched.day, matched.hour) == (slot.month, slot.day, 10)


def test_match_open_slot_rejects_unoffered_time(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    # 3:17 isn't on the 30-min grid, so nothing matches.
    assert agent._match_open_slot(db, "June 6", "3:17 PM") is None


# ── book_appointment handler ────────────────────────────────────────────────────


def test_book_requires_all_details(db: Session) -> None:
    out = agent._handle_book_appointment(
        {"date": "June 6", "time": "10:00 AM", "name": "Jane"}, "sess1234", db
    )
    assert "phone" in out.lower() and "location" in out.lower()


def test_book_success_creates_event_and_lead(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    created = {}

    def _fake_create_event(_db, *, start, summary, description, attendee_email=None):
        created["summary"] = summary
        created["description"] = description
        created["attendee"] = attendee_email
        return "https://cal/evt"

    monkeypatch.setattr(gcal, "create_event", _fake_create_event)

    slot = _future(db, 10)
    out = agent._handle_book_appointment(
        {
            "date": slot.strftime("%B %d"),
            "time": "10:00 AM",
            "name": "Jane Doe",
            "phone": "555-111-2222",
            "location": "100 Main St, Panama City FL",
            "email": "jane@example.com",
        },
        "sess1234",
        db,
    )
    assert out.startswith(agent._BOOKING_CONFIRMED_MARKER)
    # Contact details landed in the event description...
    assert "Jane Doe" in created["description"]
    assert "555-111-2222" in created["description"]
    assert "100 Main St" in created["description"]
    assert created["attendee"] == "jane@example.com"
    # ...and a prospect was saved to the pipeline.
    p = db.query(Prospect).filter(Prospect.contact_name == "Jane Doe").one()
    assert p.pipeline_stage == "qualified"
    assert p.contact_phone == "555-111-2222"


def test_book_falls_back_when_calendar_unavailable(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])

    def _boom(_db, **k):
        raise gcal.CalendarWriteError("no scope", needs_reconnect=True)

    monkeypatch.setattr(gcal, "create_event", _boom)

    slot = _future(db, 10)
    out = agent._handle_book_appointment(
        {
            "date": slot.strftime("%B %d"),
            "time": "10:00 AM",
            "name": "Bob Smith",
            "phone": "555-333-4444",
            "location": "200 Oak Ave",
        },
        "sess9999",
        db,
    )
    # Graceful: customer is told a human will confirm; lead still saved (as a lead).
    assert "confirm" in out.lower()
    p = db.query(Prospect).filter(Prospect.contact_name == "Bob Smith").one()
    assert p.pipeline_stage == "lead"


def test_book_rejects_time_not_offered(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    called = {"n": 0}
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: called.update(n=called["n"] + 1))

    out = agent._handle_book_appointment(
        {
            "date": "June 6",
            "time": "3:17 PM",  # off-grid -> no matching slot
            "name": "Jane",
            "phone": "555",
            "location": "x",
        },
        "sess1",
        db,
    )
    assert "check_availability" in out
    assert called["n"] == 0  # never tried to create an event


# ── availability filtering passthrough ─────────────────────────────────────────


def test_check_availability_passes_filters(db: Session, monkeypatch) -> None:
    seen = {}

    def _fake_msg(_db, *, weekday=None, period=None):
        seen["weekday"] = weekday
        seen["period"] = period
        return "ok"

    monkeypatch.setattr(gcal, "get_availability_message", _fake_msg)
    agent._handle_check_availability({"day": "Saturday", "period": "afternoon"}, db)
    assert seen == {"weekday": 5, "period": "afternoon"}


# ── ensure-shown guards ────────────────────────────────────────────────────────


def test_ensure_booking_shown_prepends_when_missing() -> None:
    captured = {"booking": "✅ Your consultation is confirmed for Monday at 10:00 AM CDT."}
    # Reply with no time -> confirmation gets surfaced.
    out = agent._ensure_booking_shown("Great, all set!", captured)
    assert "confirmed" in out
    # Reply that already states a time -> left as-is (no duplication).
    assert agent._ensure_booking_shown("See you Monday at 10:00 AM!", captured) == (
        "See you Monday at 10:00 AM!"
    )
