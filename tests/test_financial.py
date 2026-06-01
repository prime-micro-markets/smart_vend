from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.equipment import EquipmentUnit
from app.models.financial import MachineProForma
from app.services.financial_calc import (
    build_12month_table,
    calc_loan_payment,
    calc_summary,
    calc_unit_economics,
    cashflow_points,
    cost_breakdown,
)

# --- Pure calc unit tests ---

def test_build_12month_table_basic() -> None:
    rows = build_12month_table(
        daily_transactions=10,
        avg_ticket_usd=5.0,
        cogs_pct=0.40,
        commission_pct=0.10,
        restock_labor_monthly=100.0,
    )
    assert len(rows) == 12
    first = rows[0]
    expected_revenue = 10 * 5.0 * 30.4
    assert abs(first["revenue"] - expected_revenue) < 0.01
    assert abs(first["cogs_pct"] - 40.0) < 0.01
    assert first["cumulative"] == first["net"]


def test_build_12month_cumulative() -> None:
    rows = build_12month_table(daily_transactions=10, avg_ticket_usd=5.0, cogs_pct=0.5)
    for i, row in enumerate(rows[1:], start=1):
        assert abs(row["cumulative"] - sum(r["net"] for r in rows[: i + 1])) < 0.01


def test_calc_summary_payback() -> None:
    # Very profitable scenario — should pay back in month 1
    rows = build_12month_table(
        daily_transactions=100, avg_ticket_usd=10.0, cogs_pct=0.20
    )
    summary = calc_summary(rows, machine_cost=1000.0)
    assert summary["payback_months"] is not None
    assert summary["payback_months"] <= 12
    assert summary["total_investment"] == 1000.0
    assert summary["annual_net"] > 0


def test_calc_summary_no_payback() -> None:
    # Unprofitable scenario — net is negative
    rows = build_12month_table(
        daily_transactions=1,
        avg_ticket_usd=1.0,
        cogs_pct=0.99,
        restock_labor_monthly=500.0,
    )
    summary = calc_summary(rows, machine_cost=50_000.0)
    assert summary["payback_months"] is None


def test_calc_summary_gross_margin() -> None:
    rows = build_12month_table(daily_transactions=10, avg_ticket_usd=5.0, cogs_pct=0.30)
    summary = calc_summary(rows, machine_cost=0)
    assert abs(summary["gross_margin_pct"] - 70.0) < 0.01


def test_processing_fees_reduce_net() -> None:
    # Same scenario with and without per-transaction processing — net must drop by
    # exactly (revenue * pct + transactions * flat).
    base = {"daily_transactions": 20, "avg_ticket_usd": 4.0, "cogs_pct": 0.40}
    plain = build_12month_table(**base)
    with_fees = build_12month_table(
        **base, processing_fee_pct=0.0595, processing_fee_per_txn=0.05
    )
    txns = 20 * 30.4
    revenue = txns * 4.0
    expected_processing = revenue * 0.0595 + txns * 0.05
    assert abs(with_fees[0]["processing"] - expected_processing) < 0.01
    assert abs((plain[0]["net"] - with_fees[0]["net"]) - expected_processing) < 0.01


def test_calc_summary_daily_weekly_sales_returns() -> None:
    rows = build_12month_table(daily_transactions=20, avg_ticket_usd=4.0, cogs_pct=0.40)
    summary = calc_summary(rows, machine_cost=5000.0)
    annual_revenue = sum(r["revenue"] for r in rows)
    assert abs(summary["annual_revenue"] - annual_revenue) < 0.01
    assert abs(summary["daily_sales"] - annual_revenue / 365.0) < 0.01
    assert abs(summary["weekly_sales"] - annual_revenue / 52.0) < 0.01
    assert abs(summary["daily_net"] - summary["annual_net"] / 365.0) < 0.01
    assert abs(summary["weekly_net"] - summary["annual_net"] / 52.0) < 0.01


def test_calc_loan_payment_amortized() -> None:
    # $10,000 at 8% APR over 60 months → standard amortized payment ≈ $202.76.
    pmt = calc_loan_payment(principal=10_000.0, apr=0.08, term_months=60)
    assert abs(pmt - 202.76) < 0.5
    # Total paid exceeds principal (interest), and the formula is reversible.
    assert pmt * 60 > 10_000.0


def test_calc_loan_payment_zero_apr_is_straight_line() -> None:
    assert abs(calc_loan_payment(12_000.0, 0.0, 24) - 500.0) < 0.001


def test_calc_loan_payment_no_financing() -> None:
    # No term or no principal → no payment.
    assert calc_loan_payment(10_000.0, 0.08, 0) == 0.0
    assert calc_loan_payment(0.0, 0.08, 60) == 0.0


def test_financing_adds_monthly_cost_and_drops_upfront_machine() -> None:
    base = {"daily_transactions": 25, "avg_ticket_usd": 4.0, "cogs_pct": 0.40}
    cash = build_12month_table(**base)
    pmt = calc_loan_payment(8000.0, 0.10, 48)
    financed = build_12month_table(**base, monthly_loan_payment=pmt)
    # The loan payment is a fixed monthly cost: net drops by exactly the payment.
    assert abs((cash[0]["net"] - financed[0]["net"]) - pmt) < 0.01

    # Financed → machine cost leaves the upfront investment base.
    cash_sum = calc_summary(cash, machine_cost=8000.0, installation_cost=500.0)
    fin_sum = calc_summary(financed, machine_cost=8000.0, installation_cost=500.0, financed=True)
    assert cash_sum["total_investment"] == 8500.0
    assert fin_sum["total_investment"] == 500.0


def test_calc_unit_economics_breakeven() -> None:
    econ = calc_unit_economics(
        avg_ticket_usd=4.0,
        cogs_pct=0.40,
        commission_pct=0.10,
        processing_fee_pct=0.06,
        processing_fee_per_txn=0.05,
        fixed_monthly_opex=300.0,
    )
    # Contribution = 4*(1-0.40-0.10-0.06) - 0.05 = 4*0.44 - 0.05 = 1.71
    assert abs(econ["contribution_per_txn"] - 1.71) < 0.001
    # Break-even month = 300 / 1.71; per day = that / 30.4
    assert abs(econ["breakeven_txns_month"] - (300.0 / 1.71)) < 0.01
    assert abs(econ["breakeven_txns_day"] - (300.0 / 1.71 / 30.4)) < 0.01


def test_calc_unit_economics_negative_contribution() -> None:
    # Costs exceed the ticket → no break-even volume exists.
    econ = calc_unit_economics(avg_ticket_usd=1.0, cogs_pct=0.95, processing_fee_per_txn=0.50)
    assert econ["contribution_per_txn"] < 0
    assert econ["breakeven_txns_day"] is None


def test_cost_breakdown_segments_sum_to_revenue() -> None:
    table = build_12month_table(
        daily_transactions=20, avg_ticket_usd=4.0, cogs_pct=0.40,
        commission_pct=0.10, processing_fee_pct=0.05, restock_labor_monthly=200,
    )
    bd = cost_breakdown(table)
    assert bd["profitable"] is True
    parts = bd["cogs"] + bd["commission"] + bd["processing"] + bd["operating"] + bd["net"]
    assert abs(parts - bd["revenue"]) < 0.01
    # Profitable bar widths sum to ~100% of revenue.
    assert abs(sum(s["width"] for s in bd["segments"]) - 100.0) < 0.5


def test_cost_breakdown_loss_has_no_net_segment() -> None:
    table = build_12month_table(
        daily_transactions=1, avg_ticket_usd=1.0, cogs_pct=0.99,
        restock_labor_monthly=500,
    )
    bd = cost_breakdown(table)
    assert bd["profitable"] is False
    assert all(s["key"] != "net" for s in bd["segments"])


def test_cashflow_points_payback_detection() -> None:
    table = build_12month_table(daily_transactions=100, avg_ticket_usd=10.0, cogs_pct=0.20)
    cf = cashflow_points(table, total_investment=5000.0)
    # 13 points: month 0 (the outlay) + 12 months.
    assert len(cf["points"].split()) == 13
    assert cf["payback_month"] is not None and 1 <= cf["payback_month"] <= 12
    assert cf["final_value"] > 0


# --- HTTP endpoint tests ---

def _make_scenario(db: Session, **kwargs) -> MachineProForma:
    defaults = {
        "name": "Test Scenario",
        "machine_cost": 5000.0,
        "daily_transactions": 20.0,
        "avg_ticket_usd": 4.0,
        "cogs_pct": 0.40,
    }
    defaults.update(kwargs)
    s = MachineProForma(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_financial_index_empty(client: TestClient) -> None:
    resp = client.get("/financial/")
    assert resp.status_code == 200
    assert "Financial" in resp.text


def test_financial_index_with_scenario(client: TestClient, db: Session) -> None:
    _make_scenario(db, name="Gym Cooler")
    resp = client.get("/financial/")
    assert resp.status_code == 200
    assert "Gym Cooler" in resp.text
    assert "Net/mo" in resp.text  # per-row monthly net column renders


def test_financial_calculator_get(client: TestClient) -> None:
    resp = client.get("/financial/calculator")
    assert resp.status_code == 200


def test_financial_calculate_htmx(client: TestClient) -> None:
    resp = client.get(
        "/financial/calculate",
        params={
            "machine_cost": 5000,
            "daily_transactions": 20,
            "avg_ticket_usd": 4,
            "cogs_pct": 40,
        },
    )
    assert resp.status_code == 200
    assert "revenue" in resp.text.lower() or "Revenue" in resp.text


def test_financial_save_and_list(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/financial/calculator",
        data={
            "name": "Gym Location",
            "machine_cost": "6000",
            "daily_transactions": "25",
            "avg_ticket_usd": "4.50",
            "cogs_pct": "38",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(MachineProForma).count() == 1
    s = db.query(MachineProForma).first()
    assert s is not None
    assert s.name == "Gym Location"
    assert abs(s.cogs_pct - 0.38) < 0.001


def test_financial_detail(client: TestClient, db: Session) -> None:
    s = _make_scenario(db)
    resp = client.get(f"/financial/{s.id}")
    assert resp.status_code == 200
    assert "Test Scenario" in resp.text


def test_financial_copy(client: TestClient, db: Session) -> None:
    s = _make_scenario(db)
    resp = client.post(f"/financial/{s.id}/copy", follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(MachineProForma).count() == 2
    copy = db.query(MachineProForma).filter(MachineProForma.id != s.id).first()
    assert copy is not None
    assert "copy" in copy.name


def test_financial_delete(client: TestClient, db: Session) -> None:
    s = _make_scenario(db)
    resp = client.delete(f"/financial/{s.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(MachineProForma).count() == 0


def test_financial_404(client: TestClient) -> None:
    assert client.get("/financial/9999").status_code == 404


def test_financial_save_with_processing_fields(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/financial/calculator",
        data={
            "name": "Hospital Lobby",
            "machine_cost": "9000",
            "daily_transactions": "30",
            "avg_ticket_usd": "4.00",
            "cogs_pct": "42",
            "processing_fee_pct": "5.95",
            "processing_fee_per_txn": "0.05",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    s = db.query(MachineProForma).filter(MachineProForma.name == "Hospital Lobby").first()
    assert s is not None
    # Percent input stored as a fraction; flat fee stored verbatim.
    assert abs(s.processing_fee_pct - 0.0595) < 0.0001
    assert abs(s.processing_fee_per_txn - 0.05) < 0.0001


def test_financial_calculate_with_processing(client: TestClient) -> None:
    resp = client.get(
        "/financial/calculate",
        params={
            "machine_cost": 8000,
            "daily_transactions": 25,
            "avg_ticket_usd": 3.5,
            "cogs_pct": 40,
            "processing_fee_pct": 6,
            "processing_fee_per_txn": 0.05,
        },
    )
    assert resp.status_code == 200
    assert "Processing" in resp.text
    assert "Contribution" in resp.text
    assert "Break-even" in resp.text
    # Daily/weekly sales & returns card renders in the projection.
    assert "Daily Sales" in resp.text
    assert "Weekly Returns" in resp.text


def test_financial_save_with_manual_finance_payment(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/financial/calculator",
        data={
            "name": "Leased Cooler",
            "machine_cost": "8000",
            "daily_transactions": "25",
            "avg_ticket_usd": "4.00",
            "cogs_pct": "40",
            "finance_payment_monthly": "175",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    s = db.query(MachineProForma).filter(MachineProForma.name == "Leased Cooler").first()
    assert s is not None
    assert s.finance_payment_monthly == 175.0


def test_manual_finance_payment_overrides_amortization(client: TestClient) -> None:
    # With both a manual payment and APR/term, the entered payment is what's used.
    resp = client.get(
        "/financial/calculate",
        params={
            "machine_cost": 8000,
            "daily_transactions": 25,
            "avg_ticket_usd": 4.0,
            "cogs_pct": 40,
            "finance_apr_pct": 9,
            "finance_term_months": 48,
            "finance_payment_monthly": 250,
        },
    )
    assert resp.status_code == 200
    assert "Machine finance / month" in resp.text
    assert "$250" in resp.text


def test_financial_calculate_with_financing(client: TestClient) -> None:
    resp = client.get(
        "/financial/calculate",
        params={
            "machine_cost": 8000,
            "daily_transactions": 25,
            "avg_ticket_usd": 4.0,
            "cogs_pct": 40,
            "finance_apr_pct": 9,
            "finance_term_months": 48,
        },
    )
    assert resp.status_code == 200
    assert "Machine finance / month" in resp.text


def test_financial_save_with_financing(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/financial/calculator",
        data={
            "name": "Financed Cooler",
            "machine_cost": "8000",
            "daily_transactions": "25",
            "avg_ticket_usd": "4.00",
            "cogs_pct": "40",
            "finance_apr_pct": "9.5",
            "finance_term_months": "48",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    s = db.query(MachineProForma).filter(MachineProForma.name == "Financed Cooler").first()
    assert s is not None
    # APR stored as a fraction; term stored verbatim.
    assert abs(s.finance_apr_pct - 0.095) < 0.0001
    assert s.finance_term_months == 48


def test_calculator_lists_equipment_options(client: TestClient, db: Session) -> None:
    unit = EquipmentUnit(
        manufacturer="HAHA Vending",
        product_name="Smart Cooler X1",
        equipment_type="smart_cooler",
        price_low=8500,
        monthly_fee=39.0,
        processing_fee_pct=5.95,
    )
    db.add(unit)
    db.commit()
    resp = client.get("/financial/calculator")
    assert resp.status_code == 200
    assert "Smart Cooler X1" in resp.text
    assert 'id="equipment-picker"' in resp.text


def test_calculator_edit_renders_scenario(client: TestClient, db: Session) -> None:
    unit = EquipmentUnit(
        manufacturer="HAHA Vending",
        product_name="Cooler Z",
        equipment_type="smart_cooler",
        price_low=8000,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    s = _make_scenario(
        db,
        name="Edit Me",
        processing_fee_pct=0.0595,
        processing_fee_per_txn=0.05,
        equipment_unit_id=unit.id,
    )
    resp = client.get(f"/financial/calculator?scenario_id={s.id}")
    assert resp.status_code == 200
    assert "Edit Me" in resp.text
    assert "Based on" in resp.text  # equipment link note
    assert "Cooler Z" in resp.text


def test_save_with_equipment_link(client: TestClient, db: Session) -> None:
    unit = EquipmentUnit(
        manufacturer="Cantaloupe",
        product_name="Go Cooler",
        equipment_type="smart_cooler",
        price_low=7000,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    resp = client.post(
        "/financial/calculator",
        data={
            "name": "Linked Scenario",
            "machine_cost": "7000",
            "daily_transactions": "20",
            "avg_ticket_usd": "4.00",
            "cogs_pct": "40",
            "equipment_unit_id": str(unit.id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    s = db.query(MachineProForma).filter(MachineProForma.name == "Linked Scenario").first()
    assert s is not None and s.equipment_unit_id == unit.id


def test_financial_compare(client: TestClient, db: Session) -> None:
    a = _make_scenario(db, name="Conservative", daily_transactions=15.0)
    b = _make_scenario(db, name="Optimistic", daily_transactions=40.0)
    resp = client.get(f"/financial/compare?ids={a.id},{b.id}")
    assert resp.status_code == 200
    assert "Conservative" in resp.text
    assert "Optimistic" in resp.text
    assert "Contribution / txn" in resp.text


def test_financial_compare_ignores_bad_ids(client: TestClient, db: Session) -> None:
    a = _make_scenario(db, name="Real A")
    b = _make_scenario(db, name="Real B")
    # Non-numeric and missing ids are skipped without erroring.
    resp = client.get(f"/financial/compare?ids={a.id},abc,9999,{b.id}")
    assert resp.status_code == 200
    assert "Real A" in resp.text
    assert "Real B" in resp.text
