"""Unit tests for the market-reference fetchers (read-only competitor pricing).

External HTTP is faked so the suite stays offline and deterministic. The point
under test is the parsing + aggregation, not the live APIs.
"""

from __future__ import annotations

import pytest

from app.services import market_reference as mr


class _FakeResp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client; routes calls to a (method, url) -> _FakeResp handler."""

    def __init__(self, handler) -> None:  # noqa: ANN001
        self._handler = handler

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    def get(self, url, **kw):  # noqa: ANN001, ANN003, ANN201
        return self._handler("GET", url, kw)

    def post(self, url, **kw):  # noqa: ANN001, ANN003, ANN201
        return self._handler("POST", url, kw)


def _patch_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    monkeypatch.setattr(mr.httpx, "Client", lambda *a, **k: _FakeClient(handler))


def test_upcitemdb_parses_price_band(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "items": [
            {
                "lowest_recorded_price": 1.00,
                "highest_recorded_price": 3.00,
                "offers": [{"price": 2.00}, {"price": 2.50}],
            }
        ]
    }
    _patch_http(monkeypatch, lambda m, u, kw: _FakeResp(200, payload))
    ref = mr.fetch_upcitemdb("0 12345-67890 5")
    assert ref is not None
    assert ref.source == "upcitemdb"
    assert ref.low == 1.0
    assert ref.high == 3.0
    assert ref.avg == 2.25  # mean of offers
    assert ref.sample_count == 2
    assert ref.weak is False


def test_upcitemdb_blank_upc_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # No HTTP should even be attempted for an empty barcode.
    ref = mr.fetch_upcitemdb("   ")
    assert ref is None


def test_search_upc_returns_top_match(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"items": [{"upc": "0 12000-16115 5", "title": "Coke 12oz"}]}
    _patch_http(monkeypatch, lambda m, u, kw: _FakeResp(200, payload))
    assert mr.search_upc("Coca-Cola 12oz") == "012000161155"  # digits only


def test_search_upc_blank_query_returns_none() -> None:
    assert mr.search_upc("   ") is None


def test_search_upc_no_items_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, lambda m, u, kw: _FakeResp(200, {"items": []}))
    assert mr.search_upc("nonexistent widget") is None


def test_open_prices_aggregates_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"items": [{"price": 1.50}, {"price": 2.50}, {"price": 2.00}]}
    _patch_http(monkeypatch, lambda m, u, kw: _FakeResp(200, payload))
    ref = mr.fetch_open_prices("123456789012")
    assert ref is not None
    assert ref.low == 1.5
    assert ref.high == 2.5
    assert ref.avg == 2.0
    assert ref.sample_count == 3


def test_bls_maps_category(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "Results": {
            "series": [{"data": [{"value": "4.50", "periodName": "March", "year": "2026"}]}]
        }
    }
    _patch_http(monkeypatch, lambda m, u, kw: _FakeResp(200, payload))
    ref = mr.fetch_bls_average("snack_chips")
    assert ref is not None
    assert ref.avg == 4.5
    assert ref.source == "bls"


def test_bls_unmapped_category_returns_none() -> None:
    assert mr.fetch_bls_average("meal_sandwich") is None
    assert mr.fetch_bls_average(None) is None


def test_ebay_marks_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "_ebay_token_value", lambda: "tok")
    payload = {
        "itemSummaries": [
            {"price": {"value": "10.00"}},
            {"price": {"value": "20.00"}},
            {"price": {"value": "bad"}},  # ignored
        ]
    }
    _patch_http(monkeypatch, lambda m, u, kw: _FakeResp(200, payload))
    ref = mr.fetch_ebay("Red Bull 12oz 24pk")
    assert ref is not None
    assert ref.weak is True
    assert ref.low == 10.0
    assert ref.high == 20.0
    assert ref.avg == 15.0
    assert ref.sample_count == 2


def test_ebay_no_credentials_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr.settings, "ebay_client_id", "")
    monkeypatch.setattr(mr.settings, "ebay_client_secret", "")
    assert mr.fetch_ebay("anything") is None


def test_fetcher_swallows_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(m, u, kw):  # noqa: ANN001, ANN202
        raise RuntimeError("network down")

    _patch_http(monkeypatch, boom)
    # Defensive: a thrown request degrades to None, never propagates.
    assert mr.fetch_upcitemdb("123456789012") is None
    assert mr.fetch_open_prices("123456789012") is None


def test_gather_filters_and_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mr, "fetch_upcitemdb", lambda upc: mr.MarketReference("upcitemdb", "U", avg=2.0)
    )  # noqa: E501
    monkeypatch.setattr(mr, "fetch_open_prices", lambda upc: None)  # no data
    monkeypatch.setattr(
        mr, "fetch_bls_average", lambda cat: mr.MarketReference("bls", "B", avg=4.0)
    )  # noqa: E501
    monkeypatch.setattr(
        mr, "fetch_ebay", lambda q: mr.MarketReference("ebay", "E", avg=9.0, weak=True)
    )
    refs = mr.gather_market_reference(query="x", upc="123456789012", category="snack_chips")
    sources = [r.source for r in refs]
    assert sources == ["upcitemdb", "bls", "ebay"]  # open_prices (None) dropped, order stable


def test_gather_without_upc_skips_barcode_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"upc": False}

    def _upc(upc):  # noqa: ANN001, ANN202
        called["upc"] = True
        return None

    monkeypatch.setattr(mr, "fetch_upcitemdb", _upc)
    monkeypatch.setattr(mr, "fetch_bls_average", lambda cat: None)
    monkeypatch.setattr(mr, "fetch_ebay", lambda q: None)
    mr.gather_market_reference(query="x", upc="", category=None)
    assert called["upc"] is False  # no barcode → UPCitemdb never called
