"""Stability coverage for chatbot calendar booking: token freshness + scope handling.

These guard the two things most likely to make booking flaky in production:
the OAuth access-token refresh path, and the calendar.events scope upgrade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.models.settings import AppSetting
from app.services import gmail_monitor
from app.services import google_calendar as gcal


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


class _FakeResp:
    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or json.dumps(self._json)

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


class _FakeClient:
    """Stands in for httpx.Client as a context manager, returning queued responses."""

    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None, params=None, data=None):
        self.calls.append({"url": url, "json": json, "params": params, "data": data})
        return self._responses.pop(0)


# ── token freshness ────────────────────────────────────────────────────────────


def test_fresh_token_is_reused_without_network(db: Session, monkeypatch) -> None:
    _set(db, "gmail_refresh_token", "rt")
    _set(db, "gmail_access_token", "cached-token")
    _set(db, "gmail_token_expiry", (datetime.now(UTC) + timedelta(hours=1)).isoformat())

    def _boom(*a, **k):  # any network call here is a bug
        raise AssertionError("should not refresh a still-fresh token")

    monkeypatch.setattr(gmail_monitor.httpx, "Client", _boom)
    assert gmail_monitor.get_valid_access_token(db) == "cached-token"


def test_near_expiry_token_is_refreshed(db: Session, monkeypatch) -> None:
    # Inside the 5-minute safety margin -> must refresh rather than risk a stale token.
    _set(db, "gmail_refresh_token", "rt")
    _set(db, "gmail_access_token", "old-token")
    _set(db, "gmail_token_expiry", (datetime.now(UTC) + timedelta(minutes=2)).isoformat())

    resp = _FakeResp(json_data={"access_token": "new-token", "expires_in": 3600})
    monkeypatch.setattr(gmail_monitor.httpx, "Client", lambda *a, **k: _FakeClient([resp]))

    assert gmail_monitor.get_valid_access_token(db) == "new-token"
    # Expiry advanced well past the margin so the next call reuses it.
    new_expiry = datetime.fromisoformat(db.get(AppSetting, "gmail_token_expiry").value)
    assert new_expiry > datetime.now(UTC) + timedelta(minutes=50)


def test_refresh_persists_granted_scopes(db: Session, monkeypatch) -> None:
    _set(db, "gmail_refresh_token", "rt")
    _set(db, "gmail_token_expiry", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    scope = (
        "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/calendar.events"
    )
    resp = _FakeResp(json_data={"access_token": "t", "expires_in": 3600, "scope": scope})
    monkeypatch.setattr(gmail_monitor.httpx, "Client", lambda *a, **k: _FakeClient([resp]))

    gmail_monitor.get_valid_access_token(db)
    assert gmail_monitor.has_calendar_write(db) is True


# ── scope detection ──────────────────────────────────────────────────────────


def test_has_calendar_write_states(db: Session) -> None:
    assert gmail_monitor.has_calendar_write(db) is False  # not connected
    _set(db, "gmail_refresh_token", "rt")
    assert gmail_monitor.has_calendar_write(db) is False  # connected, scope unknown
    _set(db, "gmail_granted_scopes", "https://www.googleapis.com/auth/gmail.send")
    assert gmail_monitor.has_calendar_write(db) is False  # connected, no calendar scope
    _set(
        db,
        "gmail_granted_scopes",
        "https://www.googleapis.com/auth/gmail.send "
        "https://www.googleapis.com/auth/calendar.events",
    )
    assert gmail_monitor.has_calendar_write(db) is True


# ── event creation ───────────────────────────────────────────────────────────


def _future_weekday_10am(db: Session) -> datetime:
    cfg = gcal.load_scheduling_config(db)
    d = datetime.now(cfg.tz) + timedelta(days=1)
    while d.weekday() not in cfg.day_hours:
        d += timedelta(days=1)
    return d.replace(hour=10, minute=0, second=0, microsecond=0)


def test_create_event_success(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gmail_monitor, "get_valid_access_token", lambda _db: "tok")
    resp = _FakeResp(json_data={"htmlLink": "https://cal/evt"})
    client = _FakeClient([resp])
    monkeypatch.setattr(gcal.httpx, "Client", lambda *a, **k: client)

    link = gcal.create_event(
        db,
        start=_future_weekday_10am(db),
        summary="Consult — Jane",
        description="details",
        attendee_email="jane@example.com",
    )
    assert link == "https://cal/evt"
    assert client.calls[0]["params"].get("sendUpdates") == "all"  # invite sent


def test_create_event_scope_error_raises_and_flags(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gmail_monitor, "get_valid_access_token", lambda _db: "tok")
    resp = _FakeResp(status_code=403, text="ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    monkeypatch.setattr(gcal.httpx, "Client", lambda *a, **k: _FakeClient([resp]))

    try:
        gcal.create_event(db, start=_future_weekday_10am(db), summary="s", description="d")
        raise AssertionError("expected CalendarWriteError")
    except gcal.CalendarWriteError as exc:
        assert exc.needs_reconnect is True
    # A reconnect flag is set so the UI can prompt, without wiping Gmail tokens.
    assert db.get(AppSetting, "calendar_reauth_required") is not None


def test_create_event_retries_without_attendee_on_reject(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gmail_monitor, "get_valid_access_token", lambda _db: "tok")
    # First call (with attendee) is rejected; retry without attendees succeeds.
    bad = _FakeResp(status_code=400, text="cannot invite")
    good = _FakeResp(json_data={"htmlLink": "https://cal/evt2"})
    client = _FakeClient([bad, good])
    monkeypatch.setattr(gcal.httpx, "Client", lambda *a, **k: client)

    link = gcal.create_event(
        db,
        start=_future_weekday_10am(db),
        summary="s",
        description="d",
        attendee_email="x@example.com",
    )
    assert link == "https://cal/evt2"
    assert len(client.calls) == 2
    assert "attendees" not in client.calls[1]["json"]


# ── slot validation ──────────────────────────────────────────────────────────


def test_slot_is_available_validates(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(gcal, "_get_busy", lambda *a, **k: [])
    cfg = gcal.load_scheduling_config(db)
    good = _future_weekday_10am(db)
    assert gcal.slot_is_available(db, good) is True
    # Off the slot grid (default 30-min) -> rejected.
    assert gcal.slot_is_available(db, good.replace(minute=15)) is False
    # In the past -> rejected.
    assert gcal.slot_is_available(db, datetime.now(cfg.tz) - timedelta(hours=1)) is False
