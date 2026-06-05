from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.crm import (
    Client,
    ClientBilling,
    ClientEquipment,
    ClientInvoice,
    ClientNote,
    ClientSite,
)
from app.models.sales import Prospect
from app.views import templates

router = APIRouter(prefix="/crm", tags=["crm"])

EQUIPMENT_TYPES = ["smart_cooler", "freezer", "ambient", "kiosk", "micro_market"]
PAYMENT_METHODS = ["credit_card", "ach", "check", "invoice"]
PAYMENT_TERMS = ["prepaid", "net15", "net30", "net45"]
NOTE_TYPES = ["general", "call", "meeting", "issue", "payment", "contract"]
INVOICE_STATUSES = ["draft", "sent", "paid", "overdue", "void"]
ACCOUNT_STATUSES = ["active", "inactive", "on_hold"]
EQUIPMENT_STATUSES = ["active", "inactive", "service_needed"]


# ── Index ──────────────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
def crm_index(
    request: Request,
    q: str = "",
    status: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(Client)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Client.company_name.ilike(like)
            | Client.contact_name.ilike(like)
            | Client.contact_email.ilike(like)
            | Client.account_number.ilike(like)
        )
    if status:
        query = query.filter(Client.account_status == status)
    clients = query.order_by(Client.company_name).all()

    total_clients = db.query(func.count(Client.id)).scalar() or 0
    active_clients = (
        db.query(func.count(Client.id)).filter(Client.account_status == "active").scalar() or 0
    )
    mrr = (
        db.query(func.sum(ClientEquipment.monthly_fee))
        .join(Client)
        .filter(
            Client.account_status == "active",
            ClientEquipment.status == "active",
        )
        .scalar()
        or 0.0
    )
    pending_invoices = (
        db.query(func.count(ClientInvoice.id))
        .filter(ClientInvoice.status.in_(["sent", "overdue"]))
        .scalar()
        or 0
    )

    return templates.TemplateResponse(
        request,
        "crm/index.html",
        {
            "active_nav": "crm",
            "clients": clients,
            "q": q,
            "status_filter": status,
            "account_statuses": ACCOUNT_STATUSES,
            "total_clients": total_clients,
            "active_clients": active_clients,
            "mrr": mrr,
            "pending_invoices": pending_invoices,
        },
    )


# ── Create client ──────────────────────────────────────────────────────────────


@router.get("/new", response_class=HTMLResponse)
def client_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "crm/_client_form.html", {"client": None})


@router.post("/", response_class=HTMLResponse)
def client_create(
    request: Request,
    company_name: str = Form(...),
    contact_name: str = Form(""),
    contact_title: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    account_status: str = Form("active"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    client = Client(
        company_name=company_name,
        contact_name=contact_name or None,
        contact_title=contact_title or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        account_status=account_status,
        notes=notes or None,
    )
    db.add(client)
    db.flush()
    client.account_number = f"PMM-{client.id:04d}"
    db.commit()
    return RedirectResponse(url=f"/crm/{client.id}", status_code=303)


# ── Reports ────────────────────────────────────────────────────────────────────


@router.get("/reports", response_class=HTMLResponse)
def crm_reports(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    # MRR by client
    mrr_rows = (
        db.query(
            Client.id,
            Client.company_name,
            Client.account_number,
            func.sum(ClientEquipment.monthly_fee),
        )
        .join(ClientEquipment, ClientEquipment.client_id == Client.id)
        .filter(Client.account_status == "active", ClientEquipment.status == "active")
        .group_by(Client.id)
        .order_by(func.sum(ClientEquipment.monthly_fee).desc())
        .all()
    )

    # Revenue by equipment type
    type_rows = (
        db.query(
            ClientEquipment.equipment_type,
            func.sum(ClientEquipment.monthly_fee),
            func.count(ClientEquipment.id),
        )
        .filter(ClientEquipment.status == "active")
        .group_by(ClientEquipment.equipment_type)
        .order_by(func.sum(ClientEquipment.monthly_fee).desc())
        .all()
    )

    # Commission by client
    commission_rows = (
        db.query(
            Client.id,
            Client.company_name,
            Client.account_number,
            ClientSite.site_name,
            ClientEquipment.equipment_type,
            ClientEquipment.model_name,
            ClientEquipment.monthly_fee,
            ClientEquipment.commission_pct,
        )
        .join(ClientEquipment, ClientEquipment.client_id == Client.id)
        .join(ClientSite, ClientEquipment.site_id == ClientSite.id)
        .filter(Client.account_status == "active", ClientEquipment.status == "active")
        .order_by(Client.company_name, ClientSite.site_name)
        .all()
    )

    # Invoice summary
    invoice_summary = (
        db.query(
            ClientInvoice.status,
            func.count(ClientInvoice.id),
            func.sum(ClientInvoice.total_amount),
        )
        .group_by(ClientInvoice.status)
        .all()
    )

    total_mrr = sum(r[3] or 0 for r in mrr_rows)
    total_commission = sum(
        (r.monthly_fee or 0) * (r.commission_pct or 0) / 100 for r in commission_rows
    )

    return templates.TemplateResponse(
        request,
        "crm/reports.html",
        {
            "active_nav": "crm",
            "mrr_rows": mrr_rows,
            "type_rows": type_rows,
            "commission_rows": commission_rows,
            "invoice_summary": invoice_summary,
            "total_mrr": total_mrr,
            "total_commission": total_commission,
        },
    )


# ── Client detail ──────────────────────────────────────────────────────────────


@router.get("/{client_id}", response_class=HTMLResponse)
def client_detail(client_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    client = db.get(Client, client_id)
    if not client:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "crm/detail.html",
        {
            "active_nav": "crm",
            "client": client,
            "equipment_types": EQUIPMENT_TYPES,
            "equipment_statuses": EQUIPMENT_STATUSES,
            "payment_methods": PAYMENT_METHODS,
            "payment_terms": PAYMENT_TERMS,
            "note_types": NOTE_TYPES,
            "invoice_statuses": INVOICE_STATUSES,
            "account_statuses": ACCOUNT_STATUSES,
        },
    )


@router.get("/{client_id}/edit", response_class=HTMLResponse)
def client_edit_form(
    client_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    client = db.get(Client, client_id)
    if not client:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request, "crm/_client_form.html", {"client": client, "account_statuses": ACCOUNT_STATUSES}
    )


@router.post("/{client_id}", response_class=HTMLResponse)
def client_update(
    client_id: int,
    company_name: str = Form(...),
    contact_name: str = Form(""),
    contact_title: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    account_status: str = Form("active"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    client = db.get(Client, client_id)
    if not client:
        return Response(status_code=404)
    client.company_name = company_name
    client.contact_name = contact_name or None
    client.contact_title = contact_title or None
    client.contact_email = contact_email or None
    client.contact_phone = contact_phone or None
    client.account_status = account_status
    client.notes = notes or None
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}", status_code=303)


@router.delete("/{client_id}", response_class=HTMLResponse)
def client_delete(client_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    client = db.get(Client, client_id)
    if client:
        db.delete(client)
        db.commit()
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", status_code=200, headers={"HX-Redirect": "/crm/"})
    return RedirectResponse(url="/crm/", status_code=303)


# ── Billing ────────────────────────────────────────────────────────────────────


@router.post("/{client_id}/billing", response_class=HTMLResponse)
def client_billing_save(
    client_id: int,
    billing_email: str = Form(""),
    billing_phone: str = Form(""),
    billing_address: str = Form(""),
    billing_city: str = Form(""),
    billing_state: str = Form(""),
    billing_zip: str = Form(""),
    payment_method: str = Form(""),
    payment_terms: str = Form(""),
    auto_pay: str = Form(""),
    tax_id: str = Form(""),
    tax_exempt: str = Form(""),
    credit_limit: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    client = db.get(Client, client_id)
    if not client:
        return Response(status_code=404)
    billing = client.billing
    if not billing:
        billing = ClientBilling(client_id=client_id)
        db.add(billing)
    billing.billing_email = billing_email or None
    billing.billing_phone = billing_phone or None
    billing.billing_address = billing_address or None
    billing.billing_city = billing_city or None
    billing.billing_state = billing_state or None
    billing.billing_zip = billing_zip or None
    billing.payment_method = payment_method or None
    billing.payment_terms = payment_terms or None
    billing.auto_pay = auto_pay == "on"
    billing.tax_id = tax_id or None
    billing.tax_exempt = tax_exempt == "on"
    billing.credit_limit = float(credit_limit) if credit_limit else None
    billing.notes = notes or None
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}?tab=billing", status_code=303)


# ── Sites ──────────────────────────────────────────────────────────────────────


@router.get("/{client_id}/sites/new", response_class=HTMLResponse)
def site_new_form(client_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    client = db.get(Client, client_id)
    if not client:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request, "crm/_site_form.html", {"client": client, "site": None}
    )


@router.post("/{client_id}/sites", response_class=HTMLResponse)
def site_create(
    client_id: int,
    site_name: str = Form(...),
    address: str = Form(""),
    city: str = Form("Panama City"),
    state: str = Form("FL"),
    zip_code: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    commission_pct: str = Form(""),
    contract_start: str = Form(""),
    contract_end: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    site = ClientSite(
        client_id=client_id,
        site_name=site_name,
        address=address or None,
        city=city,
        state=state,
        zip_code=zip_code or None,
        contact_name=contact_name or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        commission_pct=float(commission_pct) if commission_pct else None,
        contract_start=date.fromisoformat(contract_start) if contract_start else None,
        contract_end=date.fromisoformat(contract_end) if contract_end else None,
        status=status,
        notes=notes or None,
    )
    db.add(site)
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}?tab=sites", status_code=303)


@router.get("/{client_id}/sites/{site_id}/edit", response_class=HTMLResponse)
def site_edit_form(
    client_id: int, site_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    client = db.get(Client, client_id)
    site = db.get(ClientSite, site_id)
    if not client or not site:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request, "crm/_site_form.html", {"client": client, "site": site}
    )


@router.post("/{client_id}/sites/{site_id}", response_class=HTMLResponse)
def site_update(
    client_id: int,
    site_id: int,
    site_name: str = Form(...),
    address: str = Form(""),
    city: str = Form("Panama City"),
    state: str = Form("FL"),
    zip_code: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    commission_pct: str = Form(""),
    contract_start: str = Form(""),
    contract_end: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    site = db.get(ClientSite, site_id)
    if not site:
        return Response(status_code=404)
    site.site_name = site_name
    site.address = address or None
    site.city = city
    site.state = state
    site.zip_code = zip_code or None
    site.contact_name = contact_name or None
    site.contact_email = contact_email or None
    site.contact_phone = contact_phone or None
    site.commission_pct = float(commission_pct) if commission_pct else None
    site.contract_start = date.fromisoformat(contract_start) if contract_start else None
    site.contract_end = date.fromisoformat(contract_end) if contract_end else None
    site.status = status
    site.notes = notes or None
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}?tab=sites", status_code=303)


@router.delete("/{client_id}/sites/{site_id}", response_class=HTMLResponse)
def site_delete(
    client_id: int, site_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    site = db.get(ClientSite, site_id)
    if site:
        db.delete(site)
        db.commit()
    if request.headers.get("HX-Request"):
        redirect = f"/crm/{client_id}?tab=sites"
        return HTMLResponse(content="", status_code=200, headers={"HX-Redirect": redirect})
    return RedirectResponse(url=f"/crm/{client_id}?tab=sites", status_code=303)


# ── Equipment ──────────────────────────────────────────────────────────────────


@router.get("/{client_id}/sites/{site_id}/equipment/new", response_class=HTMLResponse)
def equipment_new_form(
    client_id: int, site_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    client = db.get(Client, client_id)
    site = db.get(ClientSite, site_id)
    if not client or not site:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "crm/_equipment_form.html",
        {
            "client": client,
            "site": site,
            "eq": None,
            "equipment_types": EQUIPMENT_TYPES,
            "equipment_statuses": EQUIPMENT_STATUSES,
        },
    )


@router.post("/{client_id}/sites/{site_id}/equipment", response_class=HTMLResponse)
def equipment_create(
    client_id: int,
    site_id: int,
    equipment_type: str = Form(""),
    manufacturer: str = Form(""),
    model_name: str = Form(""),
    serial_number: str = Form(""),
    placement_description: str = Form(""),
    install_date: str = Form(""),
    last_service_date: str = Form(""),
    next_service_date: str = Form(""),
    monthly_fee: str = Form(""),
    commission_pct: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    eq = ClientEquipment(
        client_id=client_id,
        site_id=site_id,
        equipment_type=equipment_type or None,
        manufacturer=manufacturer or None,
        model_name=model_name or None,
        serial_number=serial_number or None,
        placement_description=placement_description or None,
        install_date=date.fromisoformat(install_date) if install_date else None,
        last_service_date=date.fromisoformat(last_service_date) if last_service_date else None,
        next_service_date=date.fromisoformat(next_service_date) if next_service_date else None,
        monthly_fee=float(monthly_fee) if monthly_fee else None,
        commission_pct=float(commission_pct) if commission_pct else None,
        status=status,
        notes=notes or None,
    )
    db.add(eq)
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}?tab=sites", status_code=303)


@router.delete("/{client_id}/equipment/{eq_id}", response_class=HTMLResponse)
def equipment_delete(
    client_id: int, eq_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    eq = db.get(ClientEquipment, eq_id)
    if eq:
        db.delete(eq)
        db.commit()
    if request.headers.get("HX-Request"):
        redirect = f"/crm/{client_id}?tab=sites"
        return HTMLResponse(content="", status_code=200, headers={"HX-Redirect": redirect})
    return RedirectResponse(url=f"/crm/{client_id}?tab=sites", status_code=303)


# ── Notes ──────────────────────────────────────────────────────────────────────


@router.post("/{client_id}/notes", response_class=HTMLResponse)
def note_create(
    client_id: int,
    request: Request,
    note_type: str = Form("general"),
    content: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = request.session.get("user", {})
    note = ClientNote(
        client_id=client_id,
        note_type=note_type,
        content=content,
        created_by=user.get("name") if user else None,
    )
    db.add(note)
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}?tab=notes", status_code=303)


# ── Invoices ───────────────────────────────────────────────────────────────────


@router.post("/{client_id}/invoices", response_class=HTMLResponse)
def invoice_create(
    client_id: int,
    site_id: str = Form(""),
    invoice_date: str = Form(...),
    due_date: str = Form(""),
    subtotal: str = Form("0"),
    tax_amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    sub = float(subtotal) if subtotal else 0.0
    tax = float(tax_amount) if tax_amount else 0.0
    inv = ClientInvoice(
        client_id=client_id,
        site_id=int(site_id) if site_id else None,
        invoice_date=date.fromisoformat(invoice_date),
        due_date=date.fromisoformat(due_date) if due_date else None,
        subtotal=sub,
        tax_amount=tax,
        total_amount=sub + tax,
        notes=notes or None,
    )
    db.add(inv)
    db.flush()
    inv.invoice_number = f"INV-{datetime.now().year}-{inv.id:04d}"
    db.commit()
    return RedirectResponse(url=f"/crm/{client_id}?tab=invoices", status_code=303)


@router.post("/invoices/{inv_id}/status", response_class=HTMLResponse)
def invoice_status_update(
    inv_id: int,
    status: str = Form(...),
    paid_amount: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    inv = db.get(ClientInvoice, inv_id)
    if not inv:
        return Response(status_code=404)
    inv.status = status
    if status == "paid":
        inv.paid_at = datetime.now()
        inv.paid_amount = float(paid_amount) if paid_amount else inv.total_amount
    db.commit()
    return RedirectResponse(url=f"/crm/{inv.client_id}?tab=invoices", status_code=303)


# ── Convert prospect to client ─────────────────────────────────────────────────


@router.post("/convert/{prospect_id}", response_class=HTMLResponse)
def convert_prospect(
    prospect_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    existing = db.query(Client).filter(Client.prospect_id == prospect_id).first()
    if existing:
        return RedirectResponse(url=f"/crm/{existing.id}", status_code=303)
    client = Client(
        company_name=prospect.company_name,
        contact_name=prospect.contact_name,
        contact_title=prospect.contact_title,
        contact_email=prospect.contact_email,
        contact_phone=prospect.contact_phone,
        account_status="active",
        prospect_id=prospect_id,
        notes=prospect.notes,
    )
    db.add(client)
    db.flush()
    client.account_number = f"PMM-{client.id:04d}"
    if prospect.address or prospect.city:
        site = ClientSite(
            client_id=client.id,
            site_name=prospect.company_name,
            address=prospect.address,
            city=prospect.city or "Panama City",
            status="active",
        )
        db.add(site)
    db.commit()
    return RedirectResponse(url=f"/crm/{client.id}", status_code=303)
