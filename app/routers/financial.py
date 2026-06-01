import json
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
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
from app.views import templates

router = APIRouter(prefix="/financial", tags=["financial"])


def _pct_to_fraction(value: float) -> float:
    """Form fields take 0–100; the engine wants a fraction. >1 means a percent."""
    return value / 100 if value > 1 else value


def _to_daily_transactions(count: float, basis: str) -> float:
    """The engine works in daily transactions; weekly input is divided across 7 days."""
    return count / 7.0 if basis == "weekly" else count


def _equipment_options(db: Session) -> list[dict[str, Any]]:
    """Active catalog units with the costs the calculator can prefill from.

    price is the cheapest sourced offer (best_source) when available, else the
    unit's denormalized price_low. processing_pct/monthly_fee feed the SaaS inputs.
    """
    units = (
        db.query(EquipmentUnit)
        .filter(EquipmentUnit.status == "active")
        .order_by(EquipmentUnit.manufacturer, EquipmentUnit.product_name)
        .all()
    )
    opts: list[dict[str, Any]] = []
    for u in units:
        best = u.best_source
        price = (best.price_low if best and best.price_low else u.price_low) or 0
        opts.append(
            {
                "id": u.id,
                "label": f"{u.manufacturer} {u.product_name}",
                "type": u.equipment_type,
                "price": price,
                "monthly_fee": u.monthly_fee or 0,
                "processing_pct": u.processing_fee_pct or 0,
            }
        )
    return opts


def _build_projection(
    *,
    daily_transactions: float,
    avg_ticket_usd: float,
    cogs_pct: float,
    commission_pct: float,
    restock_labor_monthly: float,
    supplies_monthly: float,
    insurance_monthly: float,
    other_opex_monthly: float,
    connectivity_monthly: float,
    software_monthly: float,
    recurring_restock_monthly: float,
    processing_fee_pct: float,
    processing_fee_per_txn: float,
    seasonality_json: str | None,
    machine_cost: float,
    installation_cost: float,
    initial_inventory_cost: float,
    finance_apr: float = 0.0,
    finance_term_months: float = 0.0,
    finance_payment_monthly: float = 0.0,
) -> dict[str, Any]:
    """Build the full result context (table + every summary view) from fractional rates."""
    # A directly-entered monthly payment wins; otherwise amortize machine_cost over the term.
    computed_payment = calc_loan_payment(machine_cost, finance_apr, finance_term_months)
    manual_payment = finance_payment_monthly > 0
    monthly_loan_payment = finance_payment_monthly if manual_payment else computed_payment
    financed = monthly_loan_payment > 0
    table = build_12month_table(
        daily_transactions=daily_transactions,
        avg_ticket_usd=avg_ticket_usd,
        cogs_pct=cogs_pct,
        commission_pct=commission_pct,
        restock_labor_monthly=restock_labor_monthly,
        supplies_monthly=supplies_monthly,
        insurance_monthly=insurance_monthly,
        other_opex_monthly=other_opex_monthly,
        connectivity_monthly=connectivity_monthly,
        software_monthly=software_monthly,
        recurring_restock_monthly=recurring_restock_monthly,
        processing_fee_pct=processing_fee_pct,
        processing_fee_per_txn=processing_fee_per_txn,
        monthly_loan_payment=monthly_loan_payment,
        seasonality_json=seasonality_json,
    )
    summary = calc_summary(
        table=table,
        machine_cost=machine_cost,
        installation_cost=installation_cost,
        initial_inventory_cost=initial_inventory_cost,
        financed=financed,
    )
    fixed_monthly_opex = (
        restock_labor_monthly + supplies_monthly + insurance_monthly
        + other_opex_monthly + connectivity_monthly + software_monthly
        + recurring_restock_monthly + monthly_loan_payment
    )
    unit_econ = calc_unit_economics(
        avg_ticket_usd=avg_ticket_usd,
        cogs_pct=cogs_pct,
        commission_pct=commission_pct,
        processing_fee_pct=processing_fee_pct,
        processing_fee_per_txn=processing_fee_per_txn,
        fixed_monthly_opex=fixed_monthly_opex,
    )
    finance = {
        "financed": financed,
        "monthly_payment": monthly_loan_payment,
        "manual": manual_payment,
        "apr_pct": finance_apr * 100,
        "term_months": finance_term_months,
        "principal": machine_cost,
        "total_paid": monthly_loan_payment * finance_term_months,
        "total_interest": max(monthly_loan_payment * finance_term_months - machine_cost, 0.0),
    }
    return {
        "table": table,
        "summary": summary,
        "unit_econ": unit_econ,
        "finance": finance,
        "breakdown": cost_breakdown(table),
        "cashflow": cashflow_points(table, summary["total_investment"]),
    }


def _projection_for_scenario(s: MachineProForma) -> dict[str, Any]:
    """Build a projection from a stored scenario (rates already fractional)."""
    return _build_projection(
        daily_transactions=s.daily_transactions,
        avg_ticket_usd=s.avg_ticket_usd,
        cogs_pct=s.cogs_pct,
        commission_pct=s.commission_pct,
        restock_labor_monthly=s.restock_labor_monthly,
        supplies_monthly=s.supplies_monthly,
        insurance_monthly=s.insurance_monthly,
        other_opex_monthly=s.other_opex_monthly,
        connectivity_monthly=s.connectivity_monthly,
        software_monthly=s.software_monthly,
        recurring_restock_monthly=s.recurring_restock_monthly,
        processing_fee_pct=s.processing_fee_pct,
        processing_fee_per_txn=s.processing_fee_per_txn,
        seasonality_json=s.seasonality_json,
        machine_cost=s.machine_cost,
        installation_cost=s.installation_cost,
        initial_inventory_cost=s.initial_inventory_cost,
        finance_apr=s.finance_apr_pct,
        finance_term_months=s.finance_term_months,
        finance_payment_monthly=s.finance_payment_monthly,
    )


@router.get("/", response_class=HTMLResponse)
def financial_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    scenarios = db.query(MachineProForma).order_by(MachineProForma.updated_at.desc()).all()
    # Net/mo column on the list view — cheap to compute, saves opening each scenario.
    nets = {s.id: _projection_for_scenario(s)["summary"]["avg_monthly_net"] for s in scenarios}
    return templates.TemplateResponse(
        request,
        "financial/index.html",
        {"active_nav": "financial", "scenarios": scenarios, "nets": nets},
    )


@router.get("/calculator", response_class=HTMLResponse)
def financial_calculator(
    request: Request,
    scenario_id: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    scenario = db.get(MachineProForma, scenario_id) if scenario_id else None
    season_vals = (
        json.loads(scenario.seasonality_json)
        if scenario and scenario.seasonality_json
        else [1.0] * 12
    )
    return templates.TemplateResponse(
        request,
        "financial/calculator.html",
        {
            "active_nav": "financial",
            "scenario": scenario,
            "season_vals": season_vals,
            "equipment_options": _equipment_options(db),
        },
    )


@router.get("/calculate", response_class=HTMLResponse)
def financial_calculate(
    request: Request,
    machine_cost: float = 0,
    installation_cost: float = 0,
    initial_inventory_cost: float = 0,
    daily_transactions: float = 0,
    transaction_basis: str = "daily",
    avg_ticket_usd: float = 0,
    cogs_pct: float = 0,
    commission_pct: float = 0,
    restock_labor_monthly: float = 0,
    supplies_monthly: float = 0,
    insurance_monthly: float = 0,
    other_opex_monthly: float = 0,
    connectivity_monthly: float = 0,
    software_monthly: float = 0,
    recurring_restock_monthly: float = 0,
    processing_fee_pct: float = 0,
    processing_fee_per_txn: float = 0,
    finance_apr_pct: float = 0,
    finance_term_months: float = 0,
    finance_payment_monthly: float = 0,
) -> HTMLResponse:
    season = [float(request.query_params.get(f"season_{i}", 1.0)) for i in range(12)]
    seasonality_json = json.dumps(season) if any(s != 1.0 for s in season) else None

    ctx = _build_projection(
        daily_transactions=_to_daily_transactions(daily_transactions, transaction_basis),
        avg_ticket_usd=avg_ticket_usd,
        cogs_pct=_pct_to_fraction(cogs_pct),
        commission_pct=_pct_to_fraction(commission_pct),
        restock_labor_monthly=restock_labor_monthly,
        supplies_monthly=supplies_monthly,
        insurance_monthly=insurance_monthly,
        other_opex_monthly=other_opex_monthly,
        connectivity_monthly=connectivity_monthly,
        software_monthly=software_monthly,
        recurring_restock_monthly=recurring_restock_monthly,
        processing_fee_pct=_pct_to_fraction(processing_fee_pct),
        processing_fee_per_txn=processing_fee_per_txn,
        seasonality_json=seasonality_json,
        machine_cost=machine_cost,
        installation_cost=installation_cost,
        initial_inventory_cost=initial_inventory_cost,
        finance_apr=_pct_to_fraction(finance_apr_pct),
        finance_term_months=finance_term_months,
        finance_payment_monthly=finance_payment_monthly,
    )
    return templates.TemplateResponse(request, "financial/_proforma_result.html", ctx)


@router.post("/calculator", response_class=HTMLResponse)
def financial_save(
    request: Request,
    name: str = Form(...),
    machine_cost: float = Form(...),
    installation_cost: float = Form(0),
    initial_inventory_cost: float = Form(0),
    daily_transactions: float = Form(...),
    transaction_basis: str = Form("daily"),
    avg_ticket_usd: float = Form(...),
    cogs_pct: float = Form(...),
    commission_pct: float = Form(0),
    restock_labor_monthly: float = Form(0),
    supplies_monthly: float = Form(0),
    insurance_monthly: float = Form(0),
    other_opex_monthly: float = Form(0),
    connectivity_monthly: float = Form(0),
    software_monthly: float = Form(0),
    recurring_restock_monthly: float = Form(0),
    processing_fee_pct: float = Form(0),
    processing_fee_per_txn: float = Form(0),
    finance_apr_pct: float = Form(0),
    finance_term_months: float = Form(0),
    finance_payment_monthly: float = Form(0),
    equipment_unit_id: int | None = Form(None),
    seasonality_json: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        season = json.loads(seasonality_json) if seasonality_json else None
        stored_season = json.dumps(season) if season and any(s != 1.0 for s in season) else None
    except (json.JSONDecodeError, TypeError):
        stored_season = None

    scenario = MachineProForma(
        name=name,
        machine_cost=machine_cost,
        installation_cost=installation_cost,
        initial_inventory_cost=initial_inventory_cost,
        daily_transactions=_to_daily_transactions(daily_transactions, transaction_basis),
        transaction_basis=transaction_basis,
        avg_ticket_usd=avg_ticket_usd,
        cogs_pct=_pct_to_fraction(cogs_pct),
        commission_pct=_pct_to_fraction(commission_pct),
        restock_labor_monthly=restock_labor_monthly,
        supplies_monthly=supplies_monthly,
        insurance_monthly=insurance_monthly,
        other_opex_monthly=other_opex_monthly,
        connectivity_monthly=connectivity_monthly,
        software_monthly=software_monthly,
        recurring_restock_monthly=recurring_restock_monthly,
        processing_fee_pct=_pct_to_fraction(processing_fee_pct),
        processing_fee_per_txn=processing_fee_per_txn,
        finance_apr_pct=_pct_to_fraction(finance_apr_pct),
        finance_term_months=finance_term_months,
        finance_payment_monthly=finance_payment_monthly,
        equipment_unit_id=equipment_unit_id or None,
        seasonality_json=stored_season,
        notes=notes or None,
    )
    db.add(scenario)
    db.commit()
    return RedirectResponse(url="/financial/", status_code=303)


@router.get("/compare", response_class=HTMLResponse)
def financial_compare(
    request: Request, ids: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    columns: list[dict[str, Any]] = []
    for sid in id_list:
        s = db.get(MachineProForma, sid)
        if s:
            columns.append({"scenario": s, **_projection_for_scenario(s)})
    return templates.TemplateResponse(
        request,
        "financial/compare.html",
        {"active_nav": "financial", "columns": columns},
    )


@router.get("/{scenario_id}", response_class=HTMLResponse)
def financial_detail(
    scenario_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    scenario = db.get(MachineProForma, scenario_id)
    if not scenario:
        return Response(status_code=404)
    ctx = _projection_for_scenario(scenario)
    ctx.update({"active_nav": "financial", "scenario": scenario})
    return templates.TemplateResponse(request, "financial/detail.html", ctx)


@router.post("/{scenario_id}/copy", response_class=HTMLResponse)
def financial_copy(scenario_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    original = db.get(MachineProForma, scenario_id)
    if not original:
        return Response(status_code=404)
    copy = MachineProForma(
        name=f"{original.name} (copy)",
        machine_cost=original.machine_cost,
        installation_cost=original.installation_cost,
        initial_inventory_cost=original.initial_inventory_cost,
        daily_transactions=original.daily_transactions,
        transaction_basis=original.transaction_basis,
        avg_ticket_usd=original.avg_ticket_usd,
        cogs_pct=original.cogs_pct,
        commission_pct=original.commission_pct,
        restock_labor_monthly=original.restock_labor_monthly,
        supplies_monthly=original.supplies_monthly,
        insurance_monthly=original.insurance_monthly,
        other_opex_monthly=original.other_opex_monthly,
        connectivity_monthly=original.connectivity_monthly,
        software_monthly=original.software_monthly,
        recurring_restock_monthly=original.recurring_restock_monthly,
        processing_fee_pct=original.processing_fee_pct,
        processing_fee_per_txn=original.processing_fee_per_txn,
        finance_apr_pct=original.finance_apr_pct,
        finance_term_months=original.finance_term_months,
        finance_payment_monthly=original.finance_payment_monthly,
        equipment_unit_id=original.equipment_unit_id,
        seasonality_json=original.seasonality_json,
        notes=original.notes,
    )
    db.add(copy)
    db.commit()
    return RedirectResponse(url="/financial/", status_code=303)


@router.delete("/{scenario_id}", response_class=HTMLResponse)
def financial_delete(
    scenario_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    scenario = db.get(MachineProForma, scenario_id)
    if scenario:
        db.delete(scenario)
        db.commit()
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", status_code=200, headers={"HX-Redirect": "/financial/"})
    return RedirectResponse(url="/financial/", status_code=303)
