"""Settings → Scheduling: the calendar window the chatbot offers is tunable.

Covers resolving the window from AppSetting, the parser's safety fallbacks, and
the Settings page round-trip (render + save).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.settings import AppSetting
from app.services.google_calendar import (
    _format_label,
    load_scheduling_config,
    parse_business_days,
)


def _set(db: Session, key: str, value: str) -> None:
    db.add(AppSetting(key=key, value=value))
    db.commit()


def test_parse_business_days_valid_and_junk() -> None:
    assert parse_business_days("0,1,2,3,4") == {0, 1, 2, 3, 4}
    assert parse_business_days("5, 6") == {5, 6}
    # Junk / out-of-range is ignored; empty falls back to Mon-Fri.
    assert parse_business_days("9,foo,") == {0, 1, 2, 3, 4}
    assert parse_business_days("") == {0, 1, 2, 3, 4}


def test_config_defaults(db: Session) -> None:
    cfg = load_scheduling_config(db)
    assert cfg.open_hour == 9
    assert cfg.close_hour == 17
    assert cfg.slot_minutes == 30
    assert cfg.lookahead_days == 7
    assert cfg.max_slots == 8
    assert cfg.business_days == {0, 1, 2, 3, 4}
    assert str(cfg.tz) == "America/Chicago"


def test_config_reads_overrides(db: Session) -> None:
    _set(db, "cal_open_hour", "8")
    _set(db, "cal_close_hour", "20")
    _set(db, "cal_slot_minutes", "60")
    _set(db, "cal_lookahead_days", "14")
    _set(db, "cal_max_slots", "12")
    _set(db, "cal_business_days", "0,1,2,3,4,5")
    _set(db, "cal_timezone", "America/New_York")
    cfg = load_scheduling_config(db)
    assert (cfg.open_hour, cfg.close_hour, cfg.slot_minutes) == (8, 20, 60)
    assert (cfg.lookahead_days, cfg.max_slots) == (14, 12)
    assert cfg.business_days == {0, 1, 2, 3, 4, 5}
    assert str(cfg.tz) == "America/New_York"


def test_config_clamps_and_guards(db: Session) -> None:
    # Out-of-range ints clamp; a close <= open is widened so slots still exist.
    _set(db, "cal_open_hour", "99")  # clamps to 23
    _set(db, "cal_close_hour", "2")  # 2 <= 23 -> widened
    cfg = load_scheduling_config(db)
    assert cfg.open_hour == 23
    assert cfg.close_hour > cfg.open_hour


def test_bad_timezone_falls_back(db: Session) -> None:
    _set(db, "cal_timezone", "Mars/Phobos")
    cfg = load_scheduling_config(db)
    assert str(cfg.tz) == "America/Chicago"


def test_label_uses_slot_timezone() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Eastern in January -> EST; the suffix is derived, not hardcoded "CT".
    dt = datetime(2026, 1, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    label = _format_label(dt)
    assert "10:00 AM EST" in label
    assert "CT" not in label


def test_settings_page_renders_scheduling(client: TestClient) -> None:
    resp = client.get("/settings/")
    assert resp.status_code == 200
    assert "Consultation Times" in resp.text
    assert "Days Offered" in resp.text
    assert "cal_open_hour" in resp.text
    assert "cal_timezone" in resp.text


def test_settings_save_persists_scheduling(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/settings/",
        data={
            "cal_open_hour": "8",
            "cal_close_hour": "18",
            "cal_slot_minutes": "45",
            "cal_lookahead_days": "10",
            "cal_max_slots": "6",
            "cal_day_0": "on",
            "cal_day_4": "on",
            "cal_day_5": "on",
            "cal_timezone": "America/New_York",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cfg = load_scheduling_config(db)
    assert (cfg.open_hour, cfg.close_hour, cfg.slot_minutes) == (8, 18, 45)
    assert cfg.business_days == {0, 4, 5}
    assert str(cfg.tz) == "America/New_York"


def test_settings_save_rejects_unknown_timezone(client: TestClient, db: Session) -> None:
    client.post(
        "/settings/",
        data={"cal_timezone": "Mars/Phobos", "cal_day_0": "on"},
        follow_redirects=False,
    )
    # Unknown tz is ignored, so the resolved tz stays the default.
    assert str(load_scheduling_config(db).tz) == "America/Chicago"
