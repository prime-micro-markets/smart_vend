"""Pure functions for pro-forma P&L calculations — no DB access."""

from __future__ import annotations

import json
from typing import Any

MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]

DAYS_PER_MONTH = 30.4


def build_12month_table(
    daily_transactions: float,
    avg_ticket_usd: float,
    cogs_pct: float,
    commission_pct: float = 0.0,
    restock_labor_monthly: float = 0.0,
    supplies_monthly: float = 0.0,
    insurance_monthly: float = 0.0,
    other_opex_monthly: float = 0.0,
    connectivity_monthly: float = 0.0,
    software_monthly: float = 0.0,
    processing_fee_pct: float = 0.0,
    processing_fee_per_txn: float = 0.0,
    seasonality_json: str | None = None,
) -> list[dict[str, Any]]:
    multipliers: list[float] = json.loads(seasonality_json) if seasonality_json else [1.0] * 12

    base_monthly_txns = daily_transactions * DAYS_PER_MONTH
    base_monthly_revenue = base_monthly_txns * avg_ticket_usd
    # Fixed costs don't scale with volume; commission and processing do (handled per-month).
    fixed_opex = (
        restock_labor_monthly
        + supplies_monthly
        + insurance_monthly
        + other_opex_monthly
        + connectivity_monthly
        + software_monthly
    )

    rows = []
    cumulative = 0.0
    for i, mult in enumerate(multipliers):
        revenue = base_monthly_revenue * mult
        transactions = base_monthly_txns * mult
        cogs = revenue * cogs_pct
        gross_profit = revenue - cogs
        commission = revenue * commission_pct
        # Payment processing / SaaS: a revenue share plus a flat fee per swipe.
        processing = revenue * processing_fee_pct + transactions * processing_fee_per_txn
        total_opex = fixed_opex + commission + processing
        net = gross_profit - total_opex
        cumulative += net
        rows.append(
            {
                "month": MONTHS[i],
                "multiplier": mult,
                "transactions": transactions,
                "revenue": revenue,
                "cogs": cogs,
                "gross_profit": gross_profit,
                "cogs_pct": cogs_pct * 100,
                "commission": commission,
                "processing": processing,
                "total_opex": total_opex,
                "net": net,
                "cumulative": cumulative,
            }
        )
    return rows


def calc_summary(
    table: list[dict[str, Any]],
    machine_cost: float,
    installation_cost: float = 0.0,
    initial_inventory_cost: float = 0.0,
) -> dict[str, Any]:
    total_investment = machine_cost + installation_cost + initial_inventory_cost
    annual_net = sum(r["net"] for r in table)
    avg_monthly_net = annual_net / 12 if table else 0.0

    # Payback: month when cumulative cash flow covers total investment
    payback_months: int | None = None
    running = -total_investment
    for i, row in enumerate(table):
        running += row["net"]
        if running >= 0 and payback_months is None:
            payback_months = i + 1

    steady_state_net = table[-1]["net"] if table else 0.0
    gross_margin_pct = (
        table[0]["gross_profit"] / table[0]["revenue"] * 100
        if table and table[0]["revenue"]
        else 0.0
    )

    return {
        "total_investment": total_investment,
        "annual_net": annual_net,
        "avg_monthly_net": avg_monthly_net,
        "steady_state_net": steady_state_net,
        "payback_months": payback_months,
        "gross_margin_pct": gross_margin_pct,
        "roi_pct": (annual_net / total_investment * 100) if total_investment else 0.0,
    }


def calc_unit_economics(
    avg_ticket_usd: float,
    cogs_pct: float,
    commission_pct: float = 0.0,
    processing_fee_pct: float = 0.0,
    processing_fee_per_txn: float = 0.0,
    fixed_monthly_opex: float = 0.0,
) -> dict[str, Any]:
    """Per-transaction contribution margin and the break-even volume it implies.

    Contribution per sale is what's left of the ticket after every *variable* cost:
    product (COGS), location commission, and payment processing (a % share plus a
    flat per-swipe fee). Break-even is the daily transaction count at which that
    contribution exactly covers the fixed monthly costs.
    """
    variable_pct = cogs_pct + commission_pct + processing_fee_pct
    contribution_per_txn = avg_ticket_usd * (1 - variable_pct) - processing_fee_per_txn
    contribution_margin_pct = contribution_per_txn / avg_ticket_usd * 100 if avg_ticket_usd else 0.0

    breakeven_txns_month: float | None = None
    breakeven_txns_day: float | None = None
    if contribution_per_txn > 0:
        breakeven_txns_month = fixed_monthly_opex / contribution_per_txn
        breakeven_txns_day = breakeven_txns_month / DAYS_PER_MONTH

    return {
        "contribution_per_txn": contribution_per_txn,
        "contribution_margin_pct": contribution_margin_pct,
        "fixed_monthly_opex": fixed_monthly_opex,
        "breakeven_txns_month": breakeven_txns_month,
        "breakeven_txns_day": breakeven_txns_day,
    }


# Segment styling for the cost-breakdown bar: (key, label, bootstrap bg class).
_BREAKDOWN_SEGMENTS = [
    ("cogs", "Product (COGS)", "bg-warning"),
    ("commission", "Commission", "bg-info"),
    ("processing", "Processing", "bg-purple"),
    ("operating", "Operating", "bg-secondary"),
    ("net", "Net profit", "bg-success"),
]


def cost_breakdown(table: list[dict[str, Any]]) -> dict[str, Any]:
    """Annualized 'where every revenue dollar goes' breakdown for the stacked bar.

    When the model is profitable the five segments sum to gross revenue, so each
    width is amount/revenue. When it loses money the costs exceed revenue, so the
    bar is scaled to total cash outflow instead and net is reported as a loss
    rather than drawn as a (nonexistent) green segment.
    """
    revenue = sum(r["revenue"] for r in table)
    cogs = sum(r["cogs"] for r in table)
    commission = sum(r["commission"] for r in table)
    processing = sum(r["processing"] for r in table)
    # Fixed operating costs = total opex minus the volume-driven pieces.
    operating = sum(r["total_opex"] for r in table) - commission - processing
    net = revenue - cogs - commission - processing - operating

    amounts = {
        "cogs": cogs,
        "commission": commission,
        "processing": processing,
        "operating": operating,
        "net": max(net, 0.0),
    }
    # Bar denominator: revenue when profitable, else total outflow (no net segment).
    outflow = cogs + commission + processing + operating
    denom = revenue if net >= 0 and revenue > 0 else outflow or 1.0

    segments = []
    for key, label, css in _BREAKDOWN_SEGMENTS:
        amount = amounts[key]
        if key == "net" and net < 0:
            continue
        if amount <= 0:
            continue
        segments.append(
            {
                "key": key,
                "label": label,
                "css": css,
                "amount": amount,
                "pct_of_revenue": (amount / revenue * 100) if revenue else 0.0,
                "width": amount / denom * 100,
            }
        )

    return {
        "revenue": revenue,
        "cogs": cogs,
        "commission": commission,
        "processing": processing,
        "operating": operating,
        "net": net,
        "profitable": net >= 0,
        "segments": segments,
    }


def cashflow_points(
    table: list[dict[str, Any]],
    total_investment: float,
    width: float = 320.0,
    height: float = 130.0,
    pad: float = 14.0,
) -> dict[str, Any]:
    """Geometry for an inline-SVG cumulative cash-flow chart.

    Returns a polyline-points string and the y of the break-even (zero) line so the
    template can render the chart server-side — no JS, survives HTMX swaps. The
    series starts at -investment (month 0) and adds each month's net thereafter.
    """
    # Month 0 = initial outlay; months 1..12 = outlay + running cumulative net.
    series = [-total_investment] + [-total_investment + r["cumulative"] for r in table]
    lo = min(series + [0.0])
    hi = max(series + [0.0])
    span = (hi - lo) or 1.0
    inner_h = height - 2 * pad
    inner_w = width - 2 * pad
    step = inner_w / (len(series) - 1) if len(series) > 1 else inner_w

    def y_of(v: float) -> float:
        return pad + (hi - v) / span * inner_h

    pts = [(pad + i * step, y_of(v)) for i, v in enumerate(series)]
    points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    payback_month: int | None = None
    for i, v in enumerate(series):
        if i > 0 and v >= 0:
            payback_month = i
            break

    return {
        "width": width,
        "height": height,
        "points": points_str,
        "zero_y": y_of(0.0),
        "payback_x": (pad + payback_month * step) if payback_month else None,
        "payback_month": payback_month,
        "final_value": series[-1],
        "min_value": lo,
        "max_value": hi,
    }
