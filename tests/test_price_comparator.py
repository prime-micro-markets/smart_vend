"""Tests for the price-comparison job runner: time budget, incremental
progress, and the reachable/blocked summary.

The job uses its own module-level engine (``price_comparator.engine``), so each
test points that at a fresh in-memory SQLite database and drives the real
``run_price_comparison_job`` with monkeypatched fetchers (no outbound HTTP).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers models with Base before create_all
from app.database import Base
from app.models.agent import AgentJob
from app.services import price_comparator as pc
from app.services.price_fetcher.models import PriceResult


@pytest.fixture
def pc_engine(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(pc, "engine", engine)
    return engine


def _make_job(engine, vendors: list[str]) -> int:
    with Session(engine) as s:
        job = AgentJob(
            job_type="price_comparison",
            status="pending",
            input_params=json.dumps({"product_query": "Doritos", "vendors": vendors}),
        )
        s.add(job)
        s.commit()
        return job.id


def _result() -> PriceResult:
    return PriceResult(
        vendor_key="candy_machines",
        vendor_name="CandyMachines",
        vendor_type="online_vending",
        product_name="Doritos 24pk",
        unit_price=1.25,
        source="scrape",
    )


def test_job_reachable_when_fetcher_returns(pc_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(pc._FETCHERS, "candy_machines", lambda q, cfg: [_result()])
    job_id = _make_job(pc_engine, ["candy_machines"])
    pc.run_price_comparison_job(job_id)
    with Session(pc_engine) as s:
        job = s.get(AgentJob, job_id)
        assert job.status == "done"
        summary = pc.job_summary(job)
        assert summary.get("reachable") is True
        assert summary.get("network_blocked") is False
        assert summary.get("result_count") == 1


def test_job_network_blocked_when_all_fetchers_raise(
    pc_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(q, cfg):  # noqa: ANN001, ANN202
        raise RuntimeError("connection blocked")

    monkeypatch.setitem(pc._FETCHERS, "candy_machines", boom)
    # No Anthropic key -> the AI fallback returns [] without any network call.
    monkeypatch.setattr(pc.settings, "anthropic_api_key", "")
    job_id = _make_job(pc_engine, ["candy_machines"])
    pc.run_price_comparison_job(job_id)
    with Session(pc_engine) as s:
        job = s.get(AgentJob, job_id)
        assert job.status == "done"
        summary = pc.job_summary(job)
        assert summary.get("network_blocked") is True
        assert summary.get("reachable") is False
        assert job.prospects_found == 0


def test_job_respects_time_budget(pc_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    # A negative budget is always exhausted, so the loop breaks before the first
    # fetch ever runs — proving the guard short-circuits a stalled run.
    monkeypatch.setattr(pc, "_JOB_BUDGET_SECONDS", -1)
    calls: list[int] = []

    def _fetch(q, cfg):  # noqa: ANN001, ANN202
        calls.append(1)
        return [_result()]

    monkeypatch.setitem(pc._FETCHERS, "candy_machines", _fetch)
    job_id = _make_job(pc_engine, ["candy_machines"])
    pc.run_price_comparison_job(job_id)
    with Session(pc_engine) as s:
        job = s.get(AgentJob, job_id)
        assert job.status == "done"
        events = json.loads(job.agent_log)
        assert any(e.get("event") == "budget_exhausted" for e in events)
    assert calls == []  # never dispatched a fetch


def test_job_summary_empty_without_log() -> None:
    assert pc.job_summary(None) == {}
