"""Chatbot guided booking: preference filtering, slot matching, and book_appointment.

Calendar HTTP is stubbed; these cover the agent-side logic that turns a chosen
time + contact details into a validated booking and a sales-pipeline record.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.sales import Prospect
from app.services import cs_chatbot_agent as agent
from app.services import email_sender
from app.services import google_calendar as gcal


def _book(db, monkeypatch, *, email=None, session="sess_email"):
    """Run a successful booking (calendar stubbed) and return the chosen slot."""
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: "https://cal/evt")
    slot = _future(db, 10)
    payload = {
        "date": slot.strftime("%B %d"),
        "time": "10:00 AM",
        "name": "Jane Doe",
        "phone": "555-111-2222",
        "location": "100 Main St",
    }
    if email:
        payload["email"] = email
    agent._handle_book_appointment(payload, session, db)
    return slot


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
    # June 6 2026 is a Saturday — closed in the default Mon-Fri config — so even
    # though the time parses, there's no open day to book it on.
    assert agent._match_open_slot(db, "June 6", "3:17 PM") is None


def test_match_open_slot_accepts_in_window_time_off_grid(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    # A time the customer picks between the offered 30-min slots, but still inside
    # business hours and free, is honored (this used to be bounced as "not offered").
    slot = _future(db, 10)
    off_grid = slot.replace(minute=45)  # 10:45 — off the :00/:30 grid
    matched = agent._match_open_slot(db, off_grid.strftime("%B %d"), "10:45 AM")
    assert matched is not None
    assert (matched.month, matched.day, matched.hour, matched.minute) == (
        off_grid.month,
        off_grid.day,
        10,
        45,
    )


def test_match_open_slot_still_rejects_outside_hours(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    # An in-bounds weekday but a time after the 5pm close is still refused.
    slot = _future(db, 10)
    assert agent._match_open_slot(db, slot.strftime("%B %d"), "11:30 PM") is None


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
            "date": "June 6",  # Saturday 2026 -> closed, no open slot to book
            "time": "3:17 PM",
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


def test_send_email_uses_booking_email(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email="jane@example.com", session="s_em1")
    sent = {}
    monkeypatch.setattr(
        email_sender,
        "send_email",
        lambda to, subject, body: sent.update(to=to, subject=subject, body=body),
    )
    out = agent._handle_send_confirmation_email({}, "s_em1", db)
    assert "jane@example.com" in out
    assert sent["to"] == "jane@example.com"
    assert "confirmed" in sent["body"].lower()
    assert "100 Main St" in sent["body"]  # appointment details included


def test_send_email_asks_for_address_when_missing(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email=None, session="s_em2")  # booked without email
    called = {"n": 0}
    monkeypatch.setattr(
        email_sender, "send_email", lambda *a, **k: called.update(n=called["n"] + 1)
    )
    out = agent._handle_send_confirmation_email({}, "s_em2", db)
    assert "email" in out.lower()
    assert called["n"] == 0  # nothing sent without a recipient


def test_send_email_accepts_late_provided_address(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email=None, session="s_em3")
    sent = {}
    monkeypatch.setattr(email_sender, "send_email", lambda to, subject, body: sent.update(to=to))
    out = agent._handle_send_confirmation_email({"email": "later@example.com"}, "s_em3", db)
    assert sent["to"] == "later@example.com"
    assert "later@example.com" in out


def test_send_email_without_booking(db: Session) -> None:
    out = agent._handle_send_confirmation_email({"email": "x@example.com"}, "no_booking", db)
    assert "no appointment" in out.lower()


def test_send_email_send_failure_is_graceful(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email="jane@example.com", session="s_em4")

    def _boom(*a, **k):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(email_sender, "send_email", _boom)
    out = agent._handle_send_confirmation_email({}, "s_em4", db)
    assert "couldn't send" in out.lower() or "could not send" in out.lower()


# ── deterministic confirmation email (the small-model-drift fix) ───────────────


def test_auto_send_when_visitor_provides_email_later(db: Session, monkeypatch) -> None:
    # Booked without an email; visitor supplies it on a later turn.
    _book(db, monkeypatch, email=None, session="auto1")
    sent = {}
    monkeypatch.setattr(
        email_sender, "send_email", lambda to, subject, body: sent.update(to=to, body=body)
    )
    # Even if the model's reply drifts back to scheduling, we override with a clean confirm.
    drifted = "Sure! What day works best for you — morning or afternoon?"
    out = agent._maybe_send_confirmation_email(
        "auto1", "my email is rob@example.com", drifted, {}, db
    )
    assert sent["to"] == "rob@example.com"
    assert "emailed your confirmation" in out.lower()
    assert "morning or afternoon" not in out  # the scheduling drift was replaced
    # Marked sent so it won't fire again.
    assert agent._latest_booking_record("auto1", db)["email_sent"] is True


def test_auto_send_does_not_double_send(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email=None, session="auto2")
    calls = {"n": 0}
    monkeypatch.setattr(email_sender, "send_email", lambda *a, **k: calls.update(n=calls["n"] + 1))
    agent._maybe_send_confirmation_email("auto2", "a@b.com", "x", {}, db)
    # Second turn with another email must not re-send.
    agent._maybe_send_confirmation_email("auto2", "c@d.com", "x", {}, db)
    assert calls["n"] == 1


def test_auto_send_affirmation_uses_on_file_email(db: Session, monkeypatch) -> None:
    from app.models.chat import ChatMessage

    _book(db, monkeypatch, email="onfile@example.com", session="auto3")
    # Simulate the bot having just offered to email a confirmation.
    db.add(
        ChatMessage(
            session_id="auto3",
            role="assistant",
            content="Would you like me to email you a confirmation?",
        )
    )
    db.commit()
    sent = {}
    monkeypatch.setattr(email_sender, "send_email", lambda to, subject, body: sent.update(to=to))
    out = agent._maybe_send_confirmation_email("auto3", "yes please", "ok", {}, db)
    assert sent["to"] == "onfile@example.com"
    assert "emailed your confirmation" in out.lower()


def test_auto_send_asks_for_address_when_affirmed_without_email(db: Session, monkeypatch) -> None:
    from app.models.chat import ChatMessage

    _book(db, monkeypatch, email=None, session="auto_ask")  # booked with no email on file
    db.add(
        ChatMessage(
            session_id="auto_ask",
            role="assistant",
            content="Would you like me to email you a confirmation?",
        )
    )
    db.commit()
    called = {"n": 0}
    monkeypatch.setattr(
        email_sender, "send_email", lambda *a, **k: called.update(n=called["n"] + 1)
    )
    out = agent._maybe_send_confirmation_email("auto_ask", "yes please", "ok", {}, db)
    assert "what email address" in out.lower()
    assert called["n"] == 0  # nothing sent; we asked for the address first
    assert agent._latest_booking_record("auto_ask", db).get("email_sent") is not True


def test_auto_send_skips_on_booking_turn(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email="jane@example.com", session="auto4")
    called = {"n": 0}
    monkeypatch.setattr(
        email_sender, "send_email", lambda *a, **k: called.update(n=called["n"] + 1)
    )
    # captured has "booking" => the booking just happened this turn; don't email yet.
    out = agent._maybe_send_confirmation_email(
        "auto4", "jane@example.com", "reply", {"booking": "..."}, db
    )
    assert called["n"] == 0
    assert out == "reply"


def test_auto_send_ignores_unrelated_message(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email="onfile@example.com", session="auto5")
    called = {"n": 0}
    monkeypatch.setattr(
        email_sender, "send_email", lambda *a, **k: called.update(n=called["n"] + 1)
    )
    # No email in message, no affirmation/offer context -> do nothing, leave reply alone.
    out = agent._maybe_send_confirmation_email("auto5", "what products do you stock?", "r", {}, db)
    assert called["n"] == 0
    assert out == "r"


def test_auto_send_failure_is_graceful(db: Session, monkeypatch) -> None:
    _book(db, monkeypatch, email=None, session="auto6")

    def _boom(*a, **k):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(email_sender, "send_email", _boom)
    out = agent._maybe_send_confirmation_email("auto6", "me@example.com", "x", {}, db)
    assert "couldn't email" in out.lower() or "could not email" in out.lower()
    # Not marked sent, so a later retry can still go through.
    assert agent._latest_booking_record("auto6", db).get("email_sent") is not True


def test_ensure_booking_shown_prepends_when_missing() -> None:
    captured = {"booking": "✅ Your consultation is confirmed for Monday at 10:00 AM CDT."}
    # Reply with no time -> confirmation gets surfaced.
    out = agent._ensure_booking_shown("Great, all set!", captured)
    assert "confirmed" in out
    # Reply that already states a time -> left as-is (no duplication).
    assert agent._ensure_booking_shown("See you Monday at 10:00 AM!", captured) == (
        "See you Monday at 10:00 AM!"
    )
