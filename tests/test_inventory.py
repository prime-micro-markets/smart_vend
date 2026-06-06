import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.inventory import InventoryLog, Product, ProductSource, Supplier

# Product Inventory was shelved 2026-06-05 (its programmatic best-price cost
# sourcing is unreachable until Nayax is live): the router is intentionally kept
# but no longer mounted in app.main, so every endpoint here 404s. These tests
# are skipped in lockstep with that shelving and reactivate the moment the
# router is re-mounted. See docs/inventory_overhaul_plan.md and the comment on
# the commented-out include_router(inventory.router) line in app/main.py.
pytestmark = pytest.mark.skip(reason="Product Inventory shelved 2026-06-05 (router unmounted)")


def _make_supplier(db: Session, **kwargs) -> Supplier:
    defaults = {"name": "Sysco Foods"}
    defaults.update(kwargs)
    s = Supplier(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_product(db: Session, **kwargs) -> Product:
    defaults = {
        "sku": "WATER-16OZ",
        "name": "Water 16oz",
        "unit_cost": 0.50,
        "sell_price": 1.50,
        "on_hand_qty": 0,
        "is_active": True,
    }
    defaults.update(kwargs)
    p = Product(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_inventory_index_empty(client: TestClient) -> None:
    resp = client.get("/inventory/")
    assert resp.status_code == 200
    assert "Inventory" in resp.text


def test_supplier_create(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/inventory/suppliers",
        data={"name": "Sysco Foods", "supplier_type": "distributor"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(Supplier).count() == 1
    s = db.query(Supplier).first()
    assert s is not None
    assert s.name == "Sysco Foods"


def test_suppliers_index(client: TestClient, db: Session) -> None:
    _make_supplier(db)
    resp = client.get("/inventory/suppliers")
    assert resp.status_code == 200
    assert "Sysco Foods" in resp.text


def test_product_create(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/inventory/",
        data={
            "sku": "COKE-12OZ",
            "name": "Coke 12oz",
            "unit_cost": "0.60",
            "sell_price": "2.00",
            "category": "Beverages",
            "par_level": "24",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(Product).count() == 1
    p = db.query(Product).first()
    assert p is not None
    assert p.sku == "COKE-12OZ"
    assert p.par_level == 24
    assert abs(p.sell_price - 2.00) < 0.001


def test_inventory_index_shows_products(client: TestClient, db: Session) -> None:
    _make_product(db)
    resp = client.get("/inventory/")
    assert resp.status_code == 200
    assert "Water 16oz" in resp.text


def test_inventory_category_filter(client: TestClient, db: Session) -> None:
    _make_product(db, sku="WATER-16OZ", name="Water 16oz", category="Beverages")
    _make_product(db, sku="CHIPS-1OZ", name="Chips 1oz", category="Snacks")
    resp = client.get("/inventory/?category=Beverages")
    assert resp.status_code == 200
    assert "Water 16oz" in resp.text
    assert "Chips 1oz" not in resp.text


def test_product_edit_form(client: TestClient, db: Session) -> None:
    p = _make_product(db)
    resp = client.get(f"/inventory/{p.id}/edit")
    assert resp.status_code == 200
    assert "Water 16oz" in resp.text


def test_product_update(client: TestClient, db: Session) -> None:
    p = _make_product(db)
    resp = client.post(
        f"/inventory/{p.id}",
        data={"name": "Water 20oz", "sku": "WATER-20OZ", "sell_price": "2.00", "unit_cost": "0.60"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(p)
    assert p.name == "Water 20oz"


def test_product_restock(client: TestClient, db: Session) -> None:
    p = _make_product(db, on_hand_qty=10)
    resp = client.post(
        f"/inventory/{p.id}/restock",
        data={"qty": "24", "notes": "Weekly restock"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(p)
    assert p.on_hand_qty == 34
    log = db.query(InventoryLog).first()
    assert log is not None
    assert log.qty_change == 24
    assert log.qty_after == 34
    assert log.log_type == "restock"


def test_product_deactivate(client: TestClient, db: Session) -> None:
    p = _make_product(db)
    resp = client.delete(f"/inventory/{p.id}")
    assert resp.status_code == 200
    db.refresh(p)
    assert p.is_active is False


def test_low_stock_filter(client: TestClient, db: Session) -> None:
    _make_product(db, sku="LOW-SKU", name="Low Stock Item", on_hand_qty=2, par_level=24)
    _make_product(db, sku="OK-SKU", name="OK Stock Item", on_hand_qty=50, par_level=24)
    resp = client.get("/inventory/?low_stock=true")
    assert resp.status_code == 200
    assert "Low Stock Item" in resp.text
    assert "OK Stock Item" not in resp.text


def test_product_404(client: TestClient) -> None:
    assert client.get("/inventory/9999/edit").status_code == 404


# ── Per-supplier sourcing (ProductSource) ──────────────────────────────────────


def test_product_create_with_case_pack_and_seasonal(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/inventory/",
        data={
            "sku": "GUM-1",
            "name": "Gum",
            "sell_price": "1.50",
            "case_pack_qty": "12",
            "is_seasonal": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    p = db.query(Product).filter(Product.sku == "GUM-1").first()
    assert p is not None
    assert p.case_pack_qty == 12
    assert p.is_seasonal is True


def test_effective_unit_cost_case_math(db: Session) -> None:
    s = _make_supplier(db)
    p = _make_product(db, case_pack_qty=24)
    src = ProductSource(product_id=p.id, supplier_id=s.id, case_price=18.96, case_pack_qty=24)
    db.add(src)
    db.commit()
    assert abs(src.effective_unit_cost - 0.79) < 0.001


def test_add_source_sets_best_cost(client: TestClient, db: Session) -> None:
    s = _make_supplier(db)
    p = _make_product(db, unit_cost=None, case_pack_qty=24)
    resp = client.post(
        f"/inventory/{p.id}/sources",
        data={"supplier_id": str(s.id), "case_price": "24.00", "case_pack_qty": "24"},
    )
    assert resp.status_code == 200
    db.refresh(p)
    assert p.source_count == 1
    assert abs(p.best_unit_cost - 1.00) < 0.001  # 24 / 24, derived (never stored)
    assert abs(p.effective_cost - 1.00) < 0.001


def test_adding_source_never_overwrites_hand_typed_cost(client: TestClient, db: Session) -> None:
    # The hand-entered unit_cost column must stay untouched; effective_cost is derived.
    s = _make_supplier(db)
    p = _make_product(db, unit_cost=0.50)
    client.post(f"/inventory/{p.id}/sources", data={"supplier_id": str(s.id), "unit_cost": "0.79"})
    db.refresh(p)
    assert p.unit_cost == 0.50  # column unchanged
    assert abs(p.effective_cost - 0.79) < 0.001  # but cost reflects the real supplier offer
    # Removing the only source reverts effective_cost to the hand-typed value (no stale stickiness).
    src_id = p.sources[0].id
    client.post(f"/inventory/{p.id}/sources/{src_id}/delete")
    db.refresh(p)
    assert p.source_count == 0
    assert abs(p.effective_cost - 0.50) < 0.001


def test_cheaper_source_becomes_best(client: TestClient, db: Session) -> None:
    s = _make_supplier(db, name="Sams")
    p = _make_product(db, unit_cost=None)
    client.post(f"/inventory/{p.id}/sources", data={"supplier_id": str(s.id), "unit_cost": "1.00"})
    client.post(
        f"/inventory/{p.id}/sources",
        data={"new_supplier_name": "Webstaurant", "unit_cost": "0.70"},
    )
    db.refresh(p)
    assert p.source_count == 2
    assert p.best_source.supplier.name == "Webstaurant"
    assert abs(p.best_unit_cost - 0.70) < 0.001


def test_resaving_supplier_updates_not_duplicates(client: TestClient, db: Session) -> None:
    s = _make_supplier(db)
    p = _make_product(db, unit_cost=None)
    client.post(f"/inventory/{p.id}/sources", data={"supplier_id": str(s.id), "unit_cost": "1.00"})
    client.post(f"/inventory/{p.id}/sources", data={"supplier_id": str(s.id), "unit_cost": "0.80"})
    db.refresh(p)
    assert p.source_count == 1  # same supplier upserted, not duplicated
    assert abs(p.best_unit_cost - 0.80) < 0.001


def test_toggle_preferred_source_is_exclusive(client: TestClient, db: Session) -> None:
    s = _make_supplier(db)
    p = _make_product(db)
    client.post(f"/inventory/{p.id}/sources", data={"supplier_id": str(s.id), "unit_cost": "1.00"})
    client.post(
        f"/inventory/{p.id}/sources",
        data={"new_supplier_name": "Other", "unit_cost": "1.20"},
    )
    db.refresh(p)
    first, second = p.sources[0], p.sources[1]
    client.post(f"/inventory/{p.id}/sources/{first.id}/preferred")
    client.post(f"/inventory/{p.id}/sources/{second.id}/preferred")
    db.refresh(p)
    preferred = [s for s in p.sources if s.is_preferred]
    assert len(preferred) == 1
    assert preferred[0].id == second.id


def test_delete_source_recomputes(client: TestClient, db: Session) -> None:
    s = _make_supplier(db)
    p = _make_product(db, unit_cost=None)
    client.post(f"/inventory/{p.id}/sources", data={"supplier_id": str(s.id), "unit_cost": "0.50"})
    db.refresh(p)
    src_id = p.sources[0].id
    resp = client.post(f"/inventory/{p.id}/sources/{src_id}/delete")
    assert resp.status_code == 200
    db.refresh(p)
    assert p.source_count == 0


def test_product_detail_page_renders(client: TestClient, db: Session) -> None:
    p = _make_product(db)
    resp = client.get(f"/inventory/{p.id}")
    assert resp.status_code == 200
    # v2: the "Sourcing & Price Comparison" card was split into two side-by-side
    # cards. "Market prices" is the right one (was the procurement card).
    assert "Market prices" in resp.text
    assert "Restock History" in resp.text


def test_save_source_from_comparison(client: TestClient, db: Session) -> None:
    p = _make_product(db, case_pack_qty=40, unit_cost=None)
    resp = client.post(
        f"/inventory/{p.id}/sources/from-comparison",
        data={
            "vendor_name": "Sam's Club",
            "vendor_key": "sams_club",
            "unit_price": "0.62",
            "case_price": "24.80",
            "case_qty": "40",
        },
    )
    assert resp.status_code == 200
    # Supplier auto-created from the comparator vendor
    sup = db.query(Supplier).filter(Supplier.name == "Sam's Club").first()
    assert sup is not None
    db.refresh(p)
    assert p.source_count == 1
    assert p.sources[0].origin == "comparator"
    # case math (24.80/40) wins over the single-unit price; both equal 0.62 here
    assert abs(p.effective_cost - 0.62) < 0.001
    assert p.unit_cost is None  # hand-typed column untouched


def test_restock_run_groups_below_par(client: TestClient, db: Session) -> None:
    s = _make_supplier(db, name="BestSupplier")
    low = _make_product(db, sku="LOW", name="Low Item", par_level=24, on_hand_qty=4)
    _make_product(db, sku="OK", name="Stocked Item", par_level=10, on_hand_qty=50)
    client.post(
        f"/inventory/{low.id}/sources",
        data={"supplier_id": str(s.id), "unit_cost": "1.00"},
    )
    resp = client.get("/inventory/restock-run")
    assert resp.status_code == 200
    assert "Low Item" in resp.text
    assert "Stocked Item" not in resp.text  # at/above par excluded
    assert "BestSupplier" in resp.text


def test_restock_captures_cost(client: TestClient, db: Session) -> None:
    p = _make_product(db, unit_cost=0.55, on_hand_qty=0)
    client.post(f"/inventory/{p.id}/restock", data={"qty": "12"}, follow_redirects=False)
    log = db.query(InventoryLog).filter(InventoryLog.product_id == p.id).first()
    assert log is not None
    assert log.unit_cost_at_log is not None
    assert abs(log.unit_cost_at_log - 0.55) < 0.001


def test_supplier_create_with_account_number(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/inventory/suppliers",
        data={"name": "Costco", "account_number": "MEMBER-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    s = db.query(Supplier).filter(Supplier.name == "Costco").first()
    assert s is not None
    assert s.account_number == "MEMBER-123"


# ── Supplier onboarding workflow (research task #7.2) ───────────────────────


def test_supplier_create_persists_account_status_and_priority(
    client: TestClient, db: Session
) -> None:
    resp = client.post(
        "/inventory/suppliers",
        data={
            "name": "Vistar Test",
            "account_status": "applied",
            "priority": "10",
            "address": "123 Main St",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    s = db.query(Supplier).filter(Supplier.name == "Vistar Test").first()
    assert s is not None
    assert s.account_status == "applied"
    assert s.priority == 10
    assert s.address == "123 Main St"


def test_supplier_create_normalizes_bad_status(client: TestClient, db: Session) -> None:
    """Unknown status values fall back to not_started — guard against form tampering."""
    client.post(
        "/inventory/suppliers",
        data={"name": "Bogus", "account_status": "wide_open"},
        follow_redirects=False,
    )
    s = db.query(Supplier).filter(Supplier.name == "Bogus").first()
    assert s is not None
    assert s.account_status == "not_started"


def test_status_endpoint_cycles_and_returns_banner(client: TestClient, db: Session) -> None:
    """POST /suppliers/{id}/status updates the row and re-renders the priority banner."""
    s = _make_supplier(db, name="Vistar Banner Test", priority=10)
    resp = client.post(
        f"/inventory/suppliers/{s.id}/status",
        data={"account_status": "applied"},
    )
    assert resp.status_code == 200
    # Banner partial — should mention the row since it's not yet open.
    assert "Vistar Banner Test" in resp.text
    db.refresh(s)
    assert s.account_status == "applied"

    # Cycle to open — row should drop out of the banner.
    resp = client.post(
        f"/inventory/suppliers/{s.id}/status",
        data={"account_status": "open"},
    )
    assert resp.status_code == 200
    assert "Vistar Banner Test" not in resp.text
    db.refresh(s)
    assert s.account_status == "open"


def test_priority_banner_only_shows_unopened_priority_rows(client: TestClient, db: Session) -> None:
    _make_supplier(db, name="Vistar Pri", priority=10)
    _make_supplier(db, name="Already Open", priority=20, account_status="open")
    _make_supplier(db, name="Default Pri", priority=100)
    resp = client.get("/inventory/?tab=suppliers")
    assert resp.status_code == 200
    assert "Open These Accounts" in resp.text
    assert "Vistar Pri" in resp.text
    # Banner shouldn't list opened or default-priority rows (they're in the main grid).
    banner_html = resp.text.split('id="priority-suppliers"')[1].split("</div>", 50)[0]
    assert "Already Open" not in banner_html
    assert "Default Pri" not in banner_html


# ── Supplier price import service ───────────────────────────────────────────


def test_parse_csv_text_canonicalizes_headers() -> None:
    from app.services.supplier_import import parse_csv_text

    blob = (
        "Item #,Description,Brand,Pack,Wholesale,Size\n"
        "V001,Doritos Nacho,Frito-Lay,64,$38.50,1.75oz\n"
        "V002,Lay's Classic,Frito-Lay,104,29.95,1oz\n"
    )
    rows = parse_csv_text(blob)
    assert len(rows) == 2
    assert rows[0].sku == "V001"
    assert rows[0].name == "Doritos Nacho"
    assert rows[0].case_pack_qty == 64
    assert rows[0].case_price == 38.50
    assert rows[1].unit_size == "1oz"


def test_parse_csv_text_skips_rows_missing_both_sku_and_name() -> None:
    from app.services.supplier_import import parse_csv_text

    blob = "sku,name,case_price\nV001,Doritos,38.50\n,,12.00\n"
    rows = parse_csv_text(blob)
    assert len(rows) == 1
    assert rows[0].sku == "V001"


def test_ingest_creates_products_and_sources(db: Session) -> None:
    from app.services.supplier_import import ImportRow, ingest_supplier_offers

    supplier = _make_supplier(db, name="Vistar Ingest", priority=10)
    rows = [
        ImportRow(sku="V100", name="Test Chips", brand="Frito", case_pack_qty=64, case_price=38.50),
        ImportRow(sku="V101", name="Test Soda", case_pack_qty=24, case_price=12.99),
    ]
    result = ingest_supplier_offers(db, supplier.id, rows)
    assert result.products_created == 2
    assert result.sources_created == 2
    assert result.products_updated == 0
    p = db.query(Product).filter(Product.sku == "V100").first()
    assert p is not None
    assert p.primary_supplier_id == supplier.id
    src = (
        db.query(ProductSource)
        .filter(
            ProductSource.product_id == p.id,
            ProductSource.supplier_id == supplier.id,
        )
        .first()
    )
    assert src is not None
    assert src.case_price == 38.50
    assert src.origin.endswith("_import")


def test_ingest_is_idempotent_on_re_import(db: Session) -> None:
    """Re-running the same import updates the existing source, not duplicate rows."""
    from app.services.supplier_import import ImportRow, ingest_supplier_offers

    supplier = _make_supplier(db, name="Vistar Idem", priority=10)
    rows = [ImportRow(sku="X1", name="Item One", case_pack_qty=10, case_price=10.0)]

    r1 = ingest_supplier_offers(db, supplier.id, rows)
    assert r1.products_created == 1
    assert r1.sources_created == 1

    # Re-import same rows with a new price; should update in place.
    rows[0].case_price = 11.00
    r2 = ingest_supplier_offers(db, supplier.id, rows)
    assert r2.products_created == 0
    assert r2.products_updated == 1
    assert r2.sources_created == 0
    assert r2.sources_updated == 1

    sources = db.query(ProductSource).filter(ProductSource.supplier_id == supplier.id).all()
    assert len(sources) == 1
    assert sources[0].case_price == 11.00


def test_ingest_synthesizes_sku_when_missing(db: Session) -> None:
    from app.services.supplier_import import ImportRow, ingest_supplier_offers

    supplier = _make_supplier(db, name="Goldring Gulf", priority=20)
    rows = [ImportRow(name="Red Bull 12oz", case_pack_qty=24, case_price=44.00)]
    result = ingest_supplier_offers(db, supplier.id, rows)
    assert result.products_created == 1
    p = db.query(Product).filter(Product.name == "Red Bull 12oz").first()
    assert p is not None
    # Synthetic SKU is supplier-prefixed and slugified.
    assert "red-bull" in p.sku.lower()


def test_import_route_csv_ingests_rows(client: TestClient, db: Session) -> None:
    supplier = _make_supplier(db, name="Vistar Route", priority=10)
    csv_blob = "sku,name,case_pack_qty,case_price\nRR1,Route One,12,25.00\nRR2,Route Two,24,40.00\n"
    resp = client.post(
        f"/inventory/suppliers/{supplier.id}/import",
        data={"mode": "csv", "payload": csv_blob},
    )
    assert resp.status_code == 200
    assert "Import complete" in resp.text
    assert db.query(ProductSource).filter(ProductSource.supplier_id == supplier.id).count() == 2


def test_import_route_empty_payload_shows_error(client: TestClient, db: Session) -> None:
    supplier = _make_supplier(db, name="Vistar Empty", priority=10)
    resp = client.post(
        f"/inventory/suppliers/{supplier.id}/import",
        data={"mode": "csv", "payload": "   "},
    )
    assert resp.status_code == 200
    assert "Paste a CSV" in resp.text


def test_research_task_7_2_renders_inventory_deep_link(client: TestClient, db: Session) -> None:
    """Task #7.2 gets a building-icon deep link to the Inventory > Suppliers tab."""
    from app.models.research import ResearchTask

    db.add(
        ResearchTask(
            task_number="7.2",
            section=7,
            section_name="Operational Setup",
            what="Open wholesale supplier accounts before first machine load",
            status="not_started",
            priority="high",
        )
    )
    db.commit()
    resp = client.get("/research/")
    assert resp.status_code == 200
    assert "/inventory/?tab=suppliers" in resp.text


# --- Catalog UX overhaul -----------------------------------------------------


def test_product_create_auto_slugs_blank_sku(client: TestClient, db: Session) -> None:
    """Submitting Add Product with a blank SKU derives a kebab-slug from name+brand."""
    resp = client.post(
        "/inventory/",
        data={"sku": "", "name": "Coca-Cola Classic 12oz", "brand": "Coca-Cola"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    p = db.query(Product).first()
    assert p is not None
    assert p.sku == "coca-cola-coca-cola-classic-12oz"
    assert p.name == "Coca-Cola Classic 12oz"


def test_product_create_auto_slug_dedupes_on_collision(client: TestClient, db: Session) -> None:
    """Second blank-SKU submission with the same name appends a numeric suffix."""
    payload = {"sku": "", "name": "Test Product Alpha"}
    client.post("/inventory/", data=payload, follow_redirects=False)
    client.post("/inventory/", data=payload, follow_redirects=False)
    skus = sorted(row.sku for row in db.query(Product).all())
    assert skus == ["test-product-alpha", "test-product-alpha-2"]


def test_seed_starter_route_populates_catalog(client: TestClient, db: Session) -> None:
    """The empty-state 'Seed starter catalog' button creates the canonical SKUs."""
    from app.services.starter_catalog import STARTER_PRODUCTS

    resp = client.post("/inventory/seed-starter", follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(Product).count() == len(STARTER_PRODUCTS)
    coke = db.query(Product).filter(Product.sku == "coke-12oz-can").first()
    assert coke is not None
    assert coke.brand == "Coca-Cola"
    assert coke.case_pack_qty == 24


def test_seed_starter_route_is_idempotent(client: TestClient, db: Session) -> None:
    """Re-running the seed must not duplicate rows or clobber operator edits."""
    from app.services.starter_catalog import STARTER_PRODUCTS

    client.post("/inventory/seed-starter", follow_redirects=False)
    initial_count = db.query(Product).count()
    # Operator pins a sell price on one of the seeded rows.
    coke = db.query(Product).filter(Product.sku == "coke-12oz-can").one()
    coke.sell_price = 2.50
    db.commit()

    client.post("/inventory/seed-starter", follow_redirects=False)
    assert db.query(Product).count() == initial_count == len(STARTER_PRODUCTS)
    db.refresh(coke)
    assert coke.sell_price == 2.50  # operator edit preserved


def test_compare_tab_prefills_from_product_id(client: TestClient, db: Session) -> None:
    """Catalog row → Find Prices link pre-binds the product to the comparator form."""
    p = _make_product(db, sku="RB-8-4OZ", name="Red Bull 8.4oz")
    resp = client.get(f"/inventory/?tab=compare&product_id={p.id}")
    assert resp.status_code == 200
    assert "Comparing prices for" in resp.text
    assert "Red Bull 8.4oz" in resp.text
    assert f'name="product_id" value="{p.id}"' in resp.text


def test_compare_tab_q_param_does_not_leak_sql(client: TestClient, db: Session) -> None:
    """Regression: the URL `q` parameter must not be shadowed by the products
    SQLAlchemy Query inside inventory_index. Previously `q` was reassigned to a
    Query[Product] object, and `str(Query)` returned the compiled SELECT
    statement, which then rendered into the comparator form's value attribute
    and got POSTed as the product_query — sending a SQL statement to
    Tavily/DuckDuckGo."""
    p = _make_product(db, sku="RB-8-4OZ", name="Red Bull 8.4oz")
    resp = client.get(f"/inventory/?tab=compare&product_id={p.id}&q=Red+Bull+8.4oz")
    assert resp.status_code == 200
    # The form value should be the literal product name, not a SQL statement.
    assert "SELECT products.id" not in resp.text
    assert "products_sku" not in resp.text
    # `q` should round-trip into the search input.
    assert "Red Bull 8.4oz" in resp.text


def test_web_search_truncates_overlong_query(monkeypatch) -> None:
    """Defense-in-depth: even if a giant query slips into the search dispatcher,
    it gets capped to 380 chars before hitting Tavily (which 400-char-rejects)."""
    from app.services import web_search

    captured: dict[str, str] = {}

    def fake_ddg(query: str, max_results: int) -> list[dict]:
        captured["query"] = query
        return []

    monkeypatch.setattr(web_search, "_duckduckgo", fake_ddg)
    huge = "SELECT products.id AS products_id, " * 100  # ~3.6 KB
    web_search.search(huge, max_results=3)
    assert "query" in captured
    assert len(captured["query"]) <= 380


def test_json_extract_accepts_legitimate_empty_list() -> None:
    """Regression: an LLM that returns '[]' followed by apology prose is
    saying 'no results' — the extractor must return [] not log a warning and
    return [] (the previous behavior advanced past the parsed empty list and
    failed). Verifies the firecrawl walmart 'page not accessible' case."""
    from app.services.json_extract import extract_json_list

    response = (
        "```json\n[]\n```\n\n"
        "I cannot extract product pricing information because the requested "
        "page is not accessible. The error message [says it timed out]."
    )
    result = extract_json_list(response, context="test")
    assert result == []


def test_candymachines_routes_offdomain_links_to_pseudo_vendor() -> None:
    """Off-domain hrefs in the CandyMachines fallback get their own vendor key."""
    from app.services.price_fetcher.candy_machines import _route_by_host

    assert _route_by_host("https://www.candymachines.com/product/snickers.htm") == (
        "candy_machines",
        "CandyMachines",
        "online_vending",
        "scrape",
    )
    assert _route_by_host("https://www.boxncase.com/product/snickers") == (
        "cm_ref_boxncase_com",
        "boxncase.com",
        "online_vending",
        "cm_referral",
    )
    # Empty / unparseable host → safe default to candy_machines.
    assert _route_by_host(None)[0] == "candy_machines"


# ----------------------------------------------------------------------
# Inventory v2 integration assertions
# ----------------------------------------------------------------------


def test_v2_three_tabs_only(client: TestClient) -> None:
    """Discover Suppliers was merged into Vendors in v2 — only 3 tabs remain."""
    resp = client.get("/inventory/")
    assert resp.status_code == 200
    assert "Catalog" in resp.text
    assert "Vendors" in resp.text
    assert "Find Product Prices" in resp.text
    # The old "Discover Suppliers" tab name no longer appears as a tab button.
    assert 'id="tab-sourcing"' not in resp.text


def test_v2_sourcing_tab_query_redirects_to_vendors(client: TestClient) -> None:
    """Old bookmarks targeting ?tab=sourcing land on the merged Vendors tab."""
    resp = client.get("/inventory/?tab=sourcing")
    assert resp.status_code == 200
    # The router normalizes tab=sourcing → suppliers, which makes the Vendors
    # tab pane active. The discovery form is embedded in that tab now.
    assert 'id="pane-suppliers" role="tabpanel"' in resp.text
    assert "show active" in resp.text


def test_v2_vendors_supply_checkbox_removed(client: TestClient, db: Session) -> None:
    """Vendors Supply was archived — the comparator form no longer renders it."""
    resp = client.get("/inventory/?tab=compare")
    assert resp.status_code == 200
    assert 'value="vendors_supply"' not in resp.text


def test_v2_vendors_supply_not_in_dispatcher() -> None:
    """The dispatcher map has no vendors_supply key after archiving."""
    from app.services.price_comparator import _FETCHERS, VENDOR_KEYS

    assert "vendors_supply" not in _FETCHERS
    assert "vendors_supply" not in VENDOR_KEYS


def test_walmart_fully_removed() -> None:
    """Walmart is gone from every comparator surface."""
    from app.services.price_comparator import (
        _FETCHERS,
        COMPARATOR_FETCH_KEYS,
        VENDOR_KEYS,
        _setting_keys,
    )
    from app.services.price_fetcher.models import VENDOR_META

    for collection in (_FETCHERS, VENDOR_KEYS, COMPARATOR_FETCH_KEYS, VENDOR_META):
        assert "walmart" not in collection
    assert _setting_keys("walmart") == {}


def test_sams_is_ingest_only_not_dispatched() -> None:
    """Sam's keeps its settings (Club ID/ZIP for targeting) but the comparator
    never direct-fetches it — costs arrive via the purchase-history paste import."""
    from app.services.price_comparator import (
        _FETCHERS,
        COMPARATOR_FETCH_KEYS,
        VENDOR_KEYS,
    )

    assert "sams_club" in VENDOR_KEYS  # settings still editable
    assert "sams_club" not in _FETCHERS  # not direct-fetched
    assert "sams_club" not in COMPARATOR_FETCH_KEYS  # not a checkbox


def test_v2_product_detail_splits_paid_vs_market(client: TestClient, db: Session) -> None:
    """The detail page now renders two cards side by side: What I've paid + Market prices."""
    p = _make_product(db)
    # Log a restock so the "What I've paid" card has content.
    log = InventoryLog(
        product_id=p.id,
        log_type="restock",
        qty_change=24,
        qty_after=24,
        unit_cost_at_log=0.45,
        notes="Sam's Club run",
    )
    db.add(log)
    db.commit()
    resp = client.get(f"/inventory/{p.id}")
    assert resp.status_code == 200
    assert "What I've paid" in resp.text
    assert "Market prices" in resp.text
    # Restock cost surfaces in the left card.
    assert "$0.450" in resp.text


def test_v2_bulk_source_button_only_when_unsourced(client: TestClient, db: Session) -> None:
    """The 'Source N missing' button only renders if at least one Product has
    zero ProductSources. With every SKU sourced, the button is hidden."""
    p = _make_product(db)
    sup = _make_supplier(db)
    db.add(ProductSource(product_id=p.id, supplier_id=sup.id, case_price=12.0, case_pack_qty=24))
    db.commit()
    resp = client.get("/inventory/")
    assert resp.status_code == 200
    assert "Source 1 missing" not in resp.text
    assert "Source 0 missing" not in resp.text


# ── Phase 1: fast cost entry (bulk grid + CSV import) ──────────────────────────


def test_bulk_costs_save_creates_sams_source(client: TestClient, db: Session) -> None:
    """The bulk grid saves a Sam's Club ProductSource per filled row, with the
    unit cost derived from case price / pack."""
    p = _make_product(db, unit_cost=None, case_pack_qty=None)
    resp = client.post(
        "/inventory/costs/bulk",
        data={"supplier_name": "Sam's Club", f"case_price_{p.id}": "18.96", f"pack_{p.id}": "24"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "costs_saved=1" in resp.headers["location"]
    db.refresh(p)
    assert p.source_count == 1
    src = p.sources[0]
    assert src.supplier.name == "Sam's Club"
    assert src.case_price == 18.96
    assert src.origin == "bulk_entry"
    assert abs(p.effective_cost - (18.96 / 24)) < 0.001
    # Pack backfilled onto the product for future pre-fills.
    assert p.case_pack_qty == 24


def test_bulk_costs_skips_blank_rows(client: TestClient, db: Session) -> None:
    """Rows with no/zero case price are ignored, not saved as empty sources."""
    p1 = _make_product(db, sku="A", name="Filled")
    p2 = _make_product(db, sku="B", name="Blank")
    resp = client.post(
        "/inventory/costs/bulk",
        data={
            "supplier_name": "Sam's Club",
            f"case_price_{p1.id}": "10.00",
            f"pack_{p1.id}": "20",
            f"case_price_{p2.id}": "",
            f"pack_{p2.id}": "24",
        },
        follow_redirects=False,
    )
    assert "costs_saved=1" in resp.headers["location"]
    db.refresh(p2)
    assert p2.source_count == 0


def test_cost_csv_template_downloads(client: TestClient) -> None:
    resp = client.get("/inventory/costs/import/template")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "sku,case_price,case_pack_qty" in resp.text


def test_cost_csv_import_matches_by_sku(client: TestClient, db: Session) -> None:
    _make_product(db, sku="WATER-16OZ", name="Water 16oz", unit_cost=None)
    csv_body = "sku,case_price,case_pack_qty\nwater-16oz,18.96,24\nGHOST-SKU,9.99,12\n"
    resp = client.post(
        "/inventory/costs/import",
        data={"supplier_name": "Sam's Club"},
        files={"file": ("costs.csv", csv_body.encode(), "text/csv")},
    )
    assert resp.status_code == 200
    assert "Saved costs for" in resp.text
    # The matched SKU got a source; the unknown SKU is reported unmatched.
    assert "GHOST-SKU" in resp.text
    p = db.query(Product).filter(Product.sku == "WATER-16OZ").first()
    assert p is not None
    assert p.source_count == 1
    assert abs(p.effective_cost - (18.96 / 24)) < 0.001


def test_v2_bulk_source_button_visible_when_work_to_do(client: TestClient, db: Session) -> None:
    """At least one unsourced active SKU → 'Source N missing' button appears."""
    _make_product(db)
    resp = client.get("/inventory/")
    assert resp.status_code == 200
    assert "Source 1 missing" in resp.text


# ── Phase 2: Sam's purchase-history paste import ───────────────────────────────


def test_parse_sams_paste_json() -> None:
    from app.services.sams_paste import parse_sams_paste

    blob = '[{"name":"Member\'s Mark Water 40pk","paid_price":"$3.98","qty":40}]'
    lines = parse_sams_paste(blob)
    assert len(lines) == 1
    assert lines[0].name.startswith("Member's Mark Water")
    assert lines[0].paid_price == 3.98
    assert lines[0].qty == 40


def test_parse_sams_paste_csv_export() -> None:
    from app.services.sams_paste import parse_sams_paste

    blob = "Item Description,Quantity,Price\nBottled Water 24ct,1,$18.96\nChips Variety,2,$24.50\n"
    lines = parse_sams_paste(blob)
    assert len(lines) == 2
    assert lines[0].name == "Bottled Water 24ct"
    assert lines[0].paid_price == 18.96


def test_sams_paste_preview_matches_product(client: TestClient, db: Session) -> None:
    _make_product(db, sku="WATER-16OZ", name="Bottled Water 16oz")
    blob = '[{"name":"Bottled Water 16oz Case","paid_price":18.96,"qty":24}]'
    resp = client.post("/inventory/costs/sams-paste/preview", data={"pasted": blob})
    assert resp.status_code == 200
    # The review table pre-selects the matching SKU and the parsed price.
    assert "Bottled Water 16oz" in resp.text
    assert "18.96" in resp.text


def test_sams_paste_save_creates_sams_source(client: TestClient, db: Session) -> None:
    p = _make_product(db, unit_cost=None, case_pack_qty=None)
    resp = client.post(
        "/inventory/costs/sams-paste/save",
        data={
            "save_0": "1",
            "name_0": "Bottled Water 16oz Case",
            "product_0": str(p.id),
            "price_0": "18.96",
            "pack_0": "24",
        },
    )
    assert resp.status_code == 200
    assert "Saved costs for" in resp.text
    db.refresh(p)
    assert p.source_count == 1
    src = p.sources[0]
    assert src.supplier.name == "Sam's Club"
    assert src.origin == "sams_purchase"
    assert abs(p.effective_cost - (18.96 / 24)) < 0.001


def test_sams_paste_save_skips_unchecked_or_unmatched(client: TestClient, db: Session) -> None:
    p = _make_product(db)
    resp = client.post(
        "/inventory/costs/sams-paste/save",
        data={
            # row 0 checked but no product chosen → skipped
            "save_0": "1",
            "name_0": "Mystery Item",
            "product_0": "",
            "price_0": "9.99",
            # row 1 has a product+price but is unchecked → not saved
            "name_1": "Water",
            "product_1": str(p.id),
            "price_1": "18.96",
        },
    )
    assert resp.status_code == 200
    db.refresh(p)
    assert p.source_count == 0


def test_ingest_router_and_token_removed() -> None:
    """Phase 2 retired the cross-origin token ingest plumbing."""
    import importlib

    for mod in ("app.routers.ingest", "app.services.ingest_token"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod)


# ── Phase 3: market reference (read-only competitor pricing) ───────────────────


def test_market_reference_persists_upc_and_renders(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Posting a UPC saves it onto the product and renders the reference table —
    without ever creating a cost (ProductSource) row."""
    from app.services.market_reference import MarketReference

    p = _make_product(db, upc=None)
    monkeypatch.setattr(
        "app.routers.inventory.gather_market_reference",
        lambda **kw: [MarketReference("upcitemdb", "UPCitemdb (recorded retail)", avg=2.25)],
    )
    resp = client.post(
        "/inventory/compare/market",
        data={"product_id": str(p.id), "upc": "0 12345-67890 5", "product_query": "Water"},
    )
    assert resp.status_code == 200
    assert "UPCitemdb (recorded retail)" in resp.text
    assert "$2.25" in resp.text
    db.refresh(p)
    assert p.upc == "012345678905"  # stored digits-only
    assert p.source_count == 0  # market data is never a cost


def test_market_reference_empty_shows_hint(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _make_product(db)
    monkeypatch.setattr("app.routers.inventory.gather_market_reference", lambda **kw: [])
    resp = client.post("/inventory/compare/market", data={"product_id": str(p.id)})
    assert resp.status_code == 200
    assert "No market data found" in resp.text


def test_weak_vendors_flagged_in_meta() -> None:
    from app.services.price_fetcher.models import VENDOR_META

    assert VENDOR_META["webstaurantstore"].get("weak") is True
    assert VENDOR_META["candy_machines"].get("weak") is True


# ── Phase 4: comparator results tidy (cost vs market, badges) ──────────────────


def test_compare_results_separates_cost_and_market(client: TestClient, db: Session) -> None:
    """A finished, product-bound comparison renders the cost-candidates table
    (with a weak badge on a weak vendor) and a separate market-reference panel."""
    import json as _json

    from app.models.agent import AgentJob

    p = _make_product(db, sku="DOR-1", name="Doritos Nacho")
    results = [
        {
            "vendor_key": "webstaurantstore",
            "vendor_name": "WebstaurantStore",
            "vendor_type": "online_wholesale",
            "product_name": "Doritos Nacho Case",
            "unit_price": 0.50,
            "case_price": 12.0,
            "case_qty": 24,
            "source": "scrape",
            "confidence": "medium",
        }
    ]
    job = AgentJob(
        job_type="price_comparison",
        status="done",
        input_params=_json.dumps({"product_query": "Doritos", "product_id": p.id}),
        draft_body=_json.dumps(results),
        prospects_found=1,
    )
    db.add(job)
    db.commit()

    resp = client.get(f"/inventory/compare/{job.id}")
    assert resp.status_code == 200
    assert "Supplier cost candidates" in resp.text  # cost side labeled
    assert "Market reference" in resp.text  # market side present + separate
    assert "weak" in resp.text  # weak vendor flagged


# ── UPC population: programmatic (auto + bulk) and manual single ───────────────


def test_product_create_and_update_persist_upc(client: TestClient, db: Session) -> None:
    """The product form stores a digits-only UPC on create and edit."""
    resp = client.post(
        "/inventory/",
        data={"name": "Coke 12oz", "sku": "COKE-12", "upc": "0 12000-16115 5"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    p = db.query(Product).filter(Product.sku == "COKE-12").first()
    assert p is not None and p.upc == "012000161155"
    # Edit clears it back out when blank.
    client.post("/inventory/" + str(p.id), data={"name": "Coke 12oz", "upc": ""})
    db.refresh(p)
    assert p.upc is None


def test_cost_csv_import_sets_upc_including_price_less_rows(
    client: TestClient, db: Session
) -> None:
    _make_product(db, sku="WATER-16OZ", name="Water 16oz", upc=None)
    _make_product(db, sku="COKE-12", name="Coke 12oz", upc=None)
    # Row 1 has price + upc; row 2 is UPC-only (no price) and must still set the UPC.
    csv_body = (
        "sku,case_price,case_pack_qty,upc\n"
        "WATER-16OZ,18.96,24,012000161155\n"
        "COKE-12,,,036000291452\n"
    )
    resp = client.post(
        "/inventory/costs/import",
        data={"supplier_name": "Sam's Club"},
        files={"file": ("costs.csv", csv_body.encode(), "text/csv")},
    )
    assert resp.status_code == 200
    assert "set" in resp.text.lower()  # "Also set N UPC(s)"
    w = db.query(Product).filter(Product.sku == "WATER-16OZ").first()
    c = db.query(Product).filter(Product.sku == "COKE-12").first()
    assert w.upc == "012000161155" and w.source_count == 1
    assert c.upc == "036000291452" and c.source_count == 0  # UPC-only, no cost


def test_inventory_help_documents_new_features(client: TestClient, db: Session) -> None:
    """The Catalog tab carries in-UI instructions for the new cost/UPC/market tools."""
    _make_product(db)
    resp = client.get("/inventory/")
    assert resp.status_code == 200
    body = resp.text
    assert "Quick start" in body  # numbered getting-started card
    assert "More detail" in body  # discoverable full-help toggle
    for phrase in ("Bulk Costs", "Import CSV", "Paste Sam's", "Fill UPCs", "Market reference"):
        assert phrase in body, f"help missing guidance for {phrase}"


def test_fill_missing_upcs_only_fills_blanks(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    blank = _make_product(db, sku="A", name="Doritos Nacho", upc=None)
    already = _make_product(db, sku="B", name="Lays Classic", upc="111111111111")
    monkeypatch.setattr("app.routers.inventory.search_upc", lambda q: "999999999999")
    resp = client.post("/inventory/upc/fill-missing", follow_redirects=False)
    assert resp.status_code == 303
    assert "upcs_filled=1" in resp.headers["location"]
    db.refresh(blank)
    db.refresh(already)
    assert blank.upc == "999999999999"  # filled
    assert already.upc == "111111111111"  # left untouched
