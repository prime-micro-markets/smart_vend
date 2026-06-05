"""Customer Service → Availability: per-day chatbot consultation hours.

Covers the per-day hours parser, resolving the window from AppSetting, and the
CS Availability tab routes (render + save round-trip).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.settings import AppSetting
from app.services.google_calendar import (
    _format_label,
    load_scheduling_config,
    parse_day_hours,
)


def _set(db: Session, key: str, value: str) -> None:
    db.add(AppSetting(key=key, value=value))
    db.commit()


# ── parser ─────────────────────────────────────────────────────────────────


def test_parse_day_hours_valid() -> None:
    parsed = parse_day_hours('{"0": [9, 17], "5": [10, 14]}')
    assert parsed == {0: (9, 17), 5: (10, 14)}


def test_parse_day_hours_junk_falls_back() -> None:
    # Malformed / empty input falls back to Mon-Fri 9-5 so availability is never empty by accident.
    fallback = {0: (9, 17), 1: (9, 17), 2: (9, 17), 3: (9, 17), 4: (9, 17)}
    assert parse_day_hours("not json") == fallback
    assert parse_day_hours("") == fallback
    assert parse_day_hours("[1,2,3]") == fallback


def test_parse_day_hours_drops_invalid_windows() -> None:
    # close <= open, out-of-range day, and non-numeric are dropped; the rest survive.
    parsed = parse_day_hours('{"0": [17, 9], "1": [9, 17], "9": [9, 17], "2": ["x", 17]}')
    assert parsed == {1: (9, 17)}


# ── config resolution ────────────────────────────────────────────────────────


def test_config_defaults(db: Session) -> None:
    cfg = load_scheduling_config(db)
    assert cfg.day_hours == {0: (9, 17), 1: (9, 17), 2: (9, 17), 3: (9, 17), 4: (9, 17)}
    assert cfg.slot_minutes == 30
    assert cfg.lookahead_days == 7
    assert cfg.max_slots == 8
    assert str(cfg.tz) == "America/Chicago"


def test_config_reads_overrides(db: Session) -> None:
    _set(db, "cal_day_hours", '{"4": [8, 20], "5": [10, 14]}')
    _set(db, "cal_slot_minutes", "60")
    _set(db, "cal_lookahead_days", "14")
    _set(db, "cal_max_slots", "12")
    _set(db, "cal_timezone", "America/New_York")
    cfg = load_scheduling_config(db)
    assert cfg.day_hours == {4: (8, 20), 5: (10, 14)}
    assert (cfg.slot_minutes, cfg.lookahead_days, cfg.max_slots) == (60, 14, 12)
    assert str(cfg.tz) == "America/New_York"


def test_bad_timezone_falls_back(db: Session) -> None:
    _set(db, "cal_timezone", "Mars/Phobos")
    assert str(load_scheduling_config(db).tz) == "America/Chicago"


def test_label_uses_slot_timezone() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Eastern in January -> EST; the suffix is derived, not hardcoded "CT".
    dt = datetime(2026, 1, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    label = _format_label(dt)
    assert "10:00 AM EST" in label
    assert "CT" not in label


# ── CS Availability tab routes ─────────────────────────────────────────────────


def test_availability_page_renders(client: TestClient) -> None:
    resp = client.get("/customer-service/availability")
    assert resp.status_code == 200
    assert "Consultation Availability" in resp.text
    assert "Monday" in resp.text and "Saturday" in resp.text
    assert "day_5_enabled" in resp.text
    assert "cal_timezone" in resp.text


def test_availability_save_per_day(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/customer-service/availability",
        data={
            # Mon and Sat open with different hours; other days off.
            "day_0_enabled": "1",
            "day_0_open": "9",
            "day_0_close": "17",
            "day_5_enabled": "1",
            "day_5_open": "10",
            "day_5_close": "14",
            "cal_slot_minutes": "45",
            "cal_lookahead_days": "10",
            "cal_max_slots": "6",
            "cal_timezone": "America/New_York",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    cfg = load_scheduling_config(db)
    assert cfg.day_hours == {0: (9, 17), 5: (10, 14)}
    assert (cfg.slot_minutes, cfg.lookahead_days, cfg.max_slots) == (45, 10, 6)
    assert str(cfg.tz) == "America/New_York"


def test_availability_save_drops_unchecked_and_bad_windows(client: TestClient, db: Session) -> None:
    client.post(
        "/customer-service/availability",
        data={
            "day_1_enabled": "1",
            "day_1_open": "9",
            "day_1_close": "17",
            # day_2 has hours posted but no enabled flag -> excluded.
            "day_2_open": "9",
            "day_2_close": "17",
            # day_3 enabled but close <= open -> excluded.
            "day_3_enabled": "1",
            "day_3_open": "17",
            "day_3_close": "9",
        },
        follow_redirects=False,
    )
    assert load_scheduling_config(db).day_hours == {1: (9, 17)}


def test_availability_save_rejects_unknown_timezone(client: TestClient, db: Session) -> None:
    client.post(
        "/customer-service/availability",
        data={
            "day_0_enabled": "1",
            "day_0_open": "9",
            "day_0_close": "17",
            "cal_timezone": "Mars/Phobos",
        },
        follow_redirects=False,
    )
    assert str(load_scheduling_config(db).tz) == "America/Chicago"


def test_settings_page_no_longer_has_scheduling(client: TestClient) -> None:
    # Scheduling moved to the CS Availability tab; the Settings page must not show it.
    resp = client.get("/settings/")
    assert resp.status_code == 200
    assert "Consultation Times" not in resp.text
    assert "Chatbot Scheduling" not in resp.text
