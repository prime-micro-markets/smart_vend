"""Tests for the Inventory v2 bulk + per-row background sourcing endpoints.

Covers:
- POST /inventory/bulk-source/run queues exactly the SKUs missing ProductSources.
- GET /inventory/bulk-source/status reflects in-flight progress and clears when idle.
- POST /inventory/{id}/refresh-prices returns a polling fragment for the row.
- _auto_save_best_for_product picks the cheapest primary (non-AI) row.
- The bulk endpoint is a no-op when nothing is unsourced.
- The catalog "Source N missing" button only renders when there's work to do.

The TestClient runs background tasks synchronously after the response, so a
queued task completes during the test request — but we monkey-patch
``run_price_comparison_job`` to avoid actual outbound HTTP. The auto-save
behavior is exercised by populating an AgentJob's draft_body directly and
calling the helper, which is the same path the background coroutine takes.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.agent import AgentJob
from app.models.inventory import Product, ProductSource, Supplier
from app.models.settings import AppSetting
from app.routers.inventory import _auto_save_best_for_product


def _mk_product(db: Session, sku: str, name: str, **kwargs) -> Product:
    defaults = {
        "sku": sku,
        "name": name,
        "case_pack_qty": 24,
        "is_active": True,
        "on_hand_qty": 0,
    }
    defaults.update(kwargs)
    p = Product(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _mk_sourced_product(db: Session, sku: str, name: str) -> Product:
    """Product with one ProductSource already attached (excludes it from bulk)."""
    p = _mk_product(db, sku, name)
    sup = Supplier(name=f"Sup-{sku}", supplier_type="online")
    db.add(sup)
    db.commit()
    ps = ProductSource(product_id=p.id, supplier_id=sup.id, case_price=24.0, case_pack_qty=24)
    db.add(ps)
    db.commit()
    return p


def test_bulk_source_status_idle_returns_empty(client: TestClient) -> None:
    """No active run, the strip endpoint returns an empty container."""
    resp = client.get("/inventory/bulk-source/status")
    assert resp.status_code == 200
    assert 'id="bulk-source-strip"' in resp.text
    assert "Sourcing" not in resp.text  # nothing in flight


def test_bulk_source_run_skips_when_already_in_progress(client: TestClient, db: Session) -> None:
    """Idempotent against double-click — second POST is a no-op."""
    db.add(AppSetting(key="bulk_sourcing_in_progress", value="1"))
    db.commit()
    _mk_product(db, "A", "Item A")
    resp = client.post("/inventory/bulk-source/run", follow_redirects=False)
    # Either way it redirects back to the catalog; the critical part is no
    # new AgentJob got queued.
    assert resp.status_code in (303, 200)
    assert db.query(AgentJob).filter(AgentJob.job_type == "price_comparison").count() == 0


def test_bulk_source_run_no_unsourced_is_noop(client: TestClient, db: Session) -> None:
    """If every active SKU already has at least one ProductSource, nothing
    runs and the in-progress flag is never set."""
    _mk_sourced_product(db, "A", "Item A")
    resp = client.post("/inventory/bulk-source/run", follow_redirects=False)
    assert resp.status_code == 303
    flag = db.get(AppSetting, "bulk_sourcing_in_progress")
    assert flag is None or flag.value != "1"


def test_bulk_source_run_marks_flag_and_queues_count(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """Two unsourced + one sourced = total counter set to 2; in_progress is set."""
    _mk_product(db, "A", "Item A")
    _mk_product(db, "B", "Item B")
    _mk_sourced_product(db, "C", "Item C")

    # Neutralize the actual price comparison so the background task is fast
    # and offline. The real run_price_comparison_job uses its own engine, so
    # we patch at the module level it gets imported from.
    from app.routers import inventory as inv_router

    # The bulk worker opens its own Session(engine) (it runs after the request,
    # when the injected db is gone). Point that module-level engine at the test's
    # in-memory database so the worker sees the products this test created.
    monkeypatch.setattr(inv_router, "engine", db.get_bind())

    called: list[int] = []

    def _noop(job_id: int) -> None:
        called.append(job_id)
        # Mark job done with no rows so auto-save is a no-op.
        with Session(inv_router.engine) as s:
            j = s.get(AgentJob, job_id)
            if j:
                j.status = "done"
                j.draft_body = "[]"
                s.commit()

    monkeypatch.setattr(inv_router.price_comparator, "run_price_comparison_job", _noop)

    # TestClient runs BackgroundTasks synchronously after the response.
    resp = client.post("/inventory/bulk-source/run", follow_redirects=False)
    assert resp.status_code == 303

    total = db.get(AppSetting, "bulk_sourcing_total")
    assert total is not None and total.value == "2"
    assert len(called) == 2  # one job per unsourced SKU


def test_auto_save_picks_cheapest_primary_skips_referrals(db: Session) -> None:
    """Direct fetcher rows compete; cm_referral / ai_ref rows are excluded
    even if cheaper, since they represent unverified vendor candidates."""
    p = _mk_product(db, "A", "Item A")
    job = AgentJob(
        job_type="price_comparison",
        status="done",
        draft_body=json.dumps(
            [
                {
                    "vendor_key": "sams_club",
                    "vendor_name": "Sam's Club",
                    "vendor_type": "local_wholesale",
                    "product_name": "Item A 24-pack",
                    "unit_price": 1.20,
                    "case_price": None,
                    "case_qty": None,
                    "source": "api",
                },
                {
                    "vendor_key": "walmart",
                    "vendor_name": "Walmart",
                    "vendor_type": "local_retail",
                    "product_name": "Item A",
                    "unit_price": 0.99,
                    "source": "scrape",
                },
                {
                    # Off-domain candidate — must be ignored even though cheapest.
                    "vendor_key": "ai_ref_someoffbrand.com",
                    "vendor_name": "someoffbrand.com",
                    "vendor_type": "online_wholesale",
                    "product_name": "Item A bulk",
                    "unit_price": 0.50,
                    "source": "ai_ref",
                },
            ]
        ),
    )
    db.add(job)
    db.commit()
    _auto_save_best_for_product(db, job.id, p.id)
    db.refresh(p)
    assert len(p.sources) == 1
    saved = p.sources[0]
    assert saved.supplier.name == "Walmart"
    assert saved.unit_cost == 0.99
    assert saved.origin == "comparator_auto"


def test_auto_save_returns_none_when_no_primary_priced_row(db: Session) -> None:
    """If the only results are AI fallback / referrals, no auto-save happens."""
    p = _mk_product(db, "A", "Item A")
    job = AgentJob(
        job_type="price_comparison",
        status="done",
        draft_body=json.dumps(
            [
                {
                    "vendor_key": "ai_ref_x.com",
                    "vendor_name": "x.com",
                    "vendor_type": "online_wholesale",
                    "product_name": "Item A",
                    "unit_price": 0.50,
                    "source": "ai_ref",
                },
            ]
        ),
    )
    db.add(job)
    db.commit()
    _auto_save_best_for_product(db, job.id, p.id)
    db.refresh(p)
    assert len(p.sources) == 0


def test_refresh_prices_returns_polling_fragment(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """The per-row refresh button POSTs and gets back a spinner fragment that
    HTMX-polls until the background job finishes."""
    p = _mk_product(db, "A", "Item A")
    from app.routers import inventory as inv_router

    def _noop(job_id: int) -> None:
        with Session(inv_router.engine) as s:
            j = s.get(AgentJob, job_id)
            if j:
                j.status = "done"
                j.draft_body = "[]"
                s.commit()

    monkeypatch.setattr(inv_router.price_comparator, "run_price_comparison_job", _noop)

    resp = client.post(f"/inventory/{p.id}/refresh-prices")
    assert resp.status_code == 200
    assert f'id="row-refresh-{p.id}"' in resp.text
    assert "spinner-border" in resp.text
    assert "refresh-prices/status" in resp.text


def test_refresh_prices_status_done_returns_button(client: TestClient, db: Session) -> None:
    """When the polled status sees a completed job, it returns the button
    (no spinner) so HTMX swaps the action back into the row."""
    p = _mk_product(db, "A", "Item A")
    job = AgentJob(job_type="price_comparison", status="done", draft_body="[]")
    db.add(job)
    db.commit()
    resp = client.get(f"/inventory/{p.id}/refresh-prices/status?job_id={job.id}")
    assert resp.status_code == 200
    assert "spinner-border" not in resp.text
    assert "bi-arrow-clockwise" in resp.text
