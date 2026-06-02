"""Customer Service Manager — employee portal."""

from __future__ import annotations

import json
import logging
import secrets
from collections import defaultdict
from datetime import date as date_type
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.chat import ChatMessage
from app.models.cs_governance import CSGovernanceRule
from app.models.email_approval import EmailApproval
from app.models.settings import AppSetting
from app.services import cs_manager_agent
from app.services.auth import require_user
from app.views import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customer-service", tags=["customer_service"])

# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def _gmail_connected(db: Session) -> bool:
    row = db.get(AppSetting, "gmail_refresh_token")
    return bool(row and row.value)


def _gmail_reauth_required(db: Session) -> str:
    """Return the reason string if Gmail's token was revoked, else empty.

    Populated by ``gmail_monitor._clear_stored_tokens`` when Google rejects
    the refresh grant. Cleared on the next successful ``store_tokens``.
    """
    row = db.get(AppSetting, "gmail_reauth_required")
    return row.value if row and row.value else ""


# Buckets shown under "Other / Filtered" — everything that is not customer mail.
_OTHER_CATEGORIES = ["vendor", "promotional", "internal", "spam", "other", "unclassified"]
_DEFAULT_POLL_INTERVAL = "10"


def _pending_emails_count(db: Session) -> int:
    """Pending mail that needs a human reply — customer mail only (not filtered noise)."""
    return (
        db.query(EmailApproval)
        .filter(EmailApproval.status == "pending", EmailApproval.category == "customer")
        .count()
    )


def _query_email_queue(db: Session, status: str, category: str) -> list[EmailApproval]:
    q = db.query(EmailApproval).filter(EmailApproval.status == status)
    if category == "other":
        q = q.filter(EmailApproval.category.in_(_OTHER_CATEGORIES))
    else:
        q = q.filter(EmailApproval.category == "customer")
    return q.order_by(EmailApproval.created_at.desc()).limit(50).all()


def _format_last_poll(db: Session) -> str:
    raw = _get_setting(db, "gmail_last_poll_at", "")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%b %d, %H:%M UTC")
    except ValueError:
        return ""


def _email_queue_context(
    db: Session,
    approvals: list[EmailApproval],
    status: str,
    category: str,
    poll_result: dict | None = None,
) -> dict:
    return {
        "approvals": approvals,
        "status": status,
        "category": category,
        "gmail_connected": _gmail_connected(db),
        "gmail_reauth_reason": _gmail_reauth_required(db),
        "pending_count": _pending_emails_count(db),
        "last_poll_at": _format_last_poll(db),
        "autopoll_enabled": _get_setting(db, "gmail_autopoll_enabled", "1") != "0",
        "poll_interval": _get_setting(db, "gmail_poll_interval_minutes", _DEFAULT_POLL_INTERVAL)
        or _DEFAULT_POLL_INTERVAL,
        # poll_result is only set after a manual "Poll now" so the banner shows once
        # and disappears on the next 60s auto-render. Shape: {"count": int, "error": str|None}.
        "poll_result": poll_result,
    }


def _pending_escalations_count(db: Session) -> int:
    row = db.get(AppSetting, "chatbot_escalation_pending")
    if not row or not row.value:
        return 0
    try:
        return len(json.loads(row.value))
    except Exception:
        logger.warning("Failed to parse chatbot_escalation_pending JSON")
        return 0


def _get_escalations(db: Session) -> list:
    row = db.get(AppSetting, "chatbot_escalation_pending")
    if not row or not row.value:
        return []
    try:
        return json.loads(row.value)
    except Exception:
        logger.warning("Failed to parse chatbot_escalation_pending JSON")
        return []


_QUICK_PROMPTS = [
    "What are customers asking most often?",
    "Suggest a new governance rule based on recent chats",
    "Review the current governance rules and flag any gaps",
    "How should I respond to an unhappy customer?",
    "Draft a reply to a customer asking about pricing",
]

_PROVIDERS = [
    ("anthropic", "Anthropic Claude (Paid)"),
    ("groq", "Groq / Llama (Free)"),
    ("openai", "OpenAI (Paid)"),
    ("gemini", "Google Gemini (Free tier)"),
    ("ollama", "Ollama (Local / Free)"),
]


def _ollama_running() -> bool:
    """Return True if Ollama's HTTP server responds on its configured base URL."""
    import urllib.request

    from app.config import settings as app_settings

    base = app_settings.ollama_base_url.replace("/v1", "").rstrip("/")
    try:
        urllib.request.urlopen(base, timeout=1)  # noqa: S310
        return True
    except Exception:
        return False


# ── Main page ─────────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
def cs_index(
    request: Request,
    flash_type: str = "",
    flash_msg: str = "",
    user: dict = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # Load manager chat history for the active tab
    session_id = f"manager:{user.get('email', 'employee')}"
    manager_msgs = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(["user", "assistant"]),
        )
        .order_by(ChatMessage.created_at)
        .limit(50)
        .all()
    )
    flash = {"type": flash_type, "message": flash_msg} if flash_msg else None
    chat_history = _build_chat_history_context(db)

    return templates.TemplateResponse(
        request,
        "customer_service/index.html",
        {
            "active_nav": "customer_service",
            "user": user,
            "gmail_connected": _gmail_connected(db),
            "gmail_reauth_reason": _gmail_reauth_required(db),
            "pending_emails": _pending_emails_count(db),
            "pending_escalations": _pending_escalations_count(db),
            "manager_msgs": manager_msgs,
            "quick_prompts": _QUICK_PROMPTS,
            "flash": flash,
            **chat_history,
        },
    )


# ── Governance Rules ──────────────────────────────────────────────────────────


@router.get("/governance", response_class=HTMLResponse)
def cs_governance(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rules = (
        db.query(CSGovernanceRule)
        .order_by(CSGovernanceRule.display_order, CSGovernanceRule.id)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "customer_service/_governance_rules.html",
        {"rules": rules},
    )


@router.post("/governance", response_class=HTMLResponse)
def cs_governance_create(
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    rule_text: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # Determine next display_order
    max_order = db.query(sql_func.max(CSGovernanceRule.display_order)).scalar() or 0
    rule = CSGovernanceRule(
        category=category,
        title=title.strip(),
        rule_text=rule_text.strip(),
        display_order=max_order + 1,
    )
    db.add(rule)
    db.commit()
    rules = (
        db.query(CSGovernanceRule)
        .order_by(CSGovernanceRule.display_order, CSGovernanceRule.id)
        .all()
    )
    return templates.TemplateResponse(
        request, "customer_service/_governance_list.html", {"rules": rules}
    )


@router.put("/governance/{rule_id}", response_class=HTMLResponse)
def cs_governance_update(
    rule_id: int,
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    rule_text: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rule = db.get(CSGovernanceRule, rule_id)
    if not rule:
        return Response(status_code=404)
    rule.category = category
    rule.title = title.strip()
    rule.rule_text = rule_text.strip()
    rule.updated_at = datetime.now()
    db.commit()
    rules = (
        db.query(CSGovernanceRule)
        .order_by(CSGovernanceRule.display_order, CSGovernanceRule.id)
        .all()
    )
    return templates.TemplateResponse(
        request, "customer_service/_governance_list.html", {"rules": rules}
    )


@router.delete("/governance/{rule_id}", response_class=HTMLResponse)
def cs_governance_delete(
    rule_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    rule = db.get(CSGovernanceRule, rule_id)
    if rule:
        db.delete(rule)
        db.commit()
    rules = (
        db.query(CSGovernanceRule)
        .order_by(CSGovernanceRule.display_order, CSGovernanceRule.id)
        .all()
    )
    return templates.TemplateResponse(
        request, "customer_service/_governance_list.html", {"rules": rules}
    )


@router.patch("/governance/{rule_id}/toggle", response_class=HTMLResponse)
def cs_governance_toggle(
    rule_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    rule = db.get(CSGovernanceRule, rule_id)
    if not rule:
        return Response(status_code=404)
    rule.is_active = not rule.is_active
    rule.updated_at = datetime.now()
    db.commit()
    rules = (
        db.query(CSGovernanceRule)
        .order_by(CSGovernanceRule.display_order, CSGovernanceRule.id)
        .all()
    )
    return templates.TemplateResponse(
        request, "customer_service/_governance_list.html", {"rules": rules}
    )


# ── Email Queue ───────────────────────────────────────────────────────────────


@router.get("/email-queue", response_class=HTMLResponse)
def cs_email_queue(
    request: Request,
    status: str = "pending",
    category: str = "customer",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    approvals = _query_email_queue(db, status, category)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_queue.html",
        _email_queue_context(db, approvals, status, category),
    )


@router.post("/email-queue/{approval_id}/approve", response_class=HTMLResponse)
def cs_email_approve(
    approval_id: int,
    request: Request,
    user: dict = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    approval = db.get(EmailApproval, approval_id)
    if not approval or approval.status != "pending":
        return Response(status_code=404)

    try:
        from app.services import gmail_monitor

        gmail_monitor.send_reply_via_gmail_api(approval, db)
        approval.status = "sent"
        approval.reviewed_by = user.get("email", "")
        approval.reviewed_at = datetime.now()
        approval.sent_at = datetime.now()
    except Exception as exc:
        approval.status = "approved"
        approval.reviewed_by = user.get("email", "")
        approval.reviewed_at = datetime.now()
        approval.review_notes = f"Send failed: {exc}"
    db.commit()
    db.refresh(approval)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_card.html",
        {"a": approval, "gmail_connected": _gmail_connected(db)},
    )


@router.post("/email-queue/{approval_id}/reject", response_class=HTMLResponse)
def cs_email_reject(
    approval_id: int,
    request: Request,
    user: dict = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    approval = db.get(EmailApproval, approval_id)
    if not approval:
        return Response(status_code=404)
    approval.status = "rejected"
    approval.reviewed_by = user.get("email", "")
    approval.reviewed_at = datetime.now()
    db.commit()
    db.refresh(approval)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_card.html",
        {"a": approval, "gmail_connected": _gmail_connected(db)},
    )


@router.post("/email-queue/{approval_id}/edit-draft", response_class=HTMLResponse)
def cs_email_edit_draft(
    approval_id: int,
    request: Request,
    draft_subject: str = Form(...),
    draft_body: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    approval = db.get(EmailApproval, approval_id)
    if not approval:
        return Response(status_code=404)
    approval.draft_subject = draft_subject.strip()
    approval.draft_body = draft_body.strip()
    db.commit()
    db.refresh(approval)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_card.html",
        {"a": approval, "gmail_connected": _gmail_connected(db)},
    )


@router.post("/email-queue/{approval_id}/generate-draft", response_class=HTMLResponse)
def cs_generate_draft(
    approval_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from app.services import cs_email_agent

    approval = db.get(EmailApproval, approval_id)
    if not approval:
        return Response(status_code=404)
    cs_email_agent.draft_reply(approval, db)
    db.refresh(approval)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_card.html",
        {"a": approval, "gmail_connected": _gmail_connected(db)},
    )


@router.delete("/email-queue/{approval_id}", response_class=HTMLResponse)
def cs_email_delete(
    approval_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    approval = db.get(EmailApproval, approval_id)
    if approval:
        db.delete(approval)
        db.commit()
    new_count = _pending_emails_count(db)
    badge_inner = f'<span class="badge bg-danger ms-1">{new_count}</span>' if new_count > 0 else ""
    oob = f'<span id="email-tab-badge" hx-swap-oob="true">{badge_inner}</span>'
    return HTMLResponse(oob)


@router.post("/email-queue/clear-resolved", response_class=HTMLResponse)
def cs_email_clear_resolved(
    request: Request,
    status: str = "pending",
    category: str = "customer",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    db.query(EmailApproval).filter(EmailApproval.status.in_(["sent", "rejected"])).delete(
        synchronize_session=False
    )
    db.commit()
    approvals = _query_email_queue(db, status, category)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_queue.html",
        _email_queue_context(db, approvals, status, category),
    )


@router.post("/email-queue/{approval_id}/reclassify", response_class=HTMLResponse)
def cs_email_reclassify(
    approval_id: int,
    request: Request,
    category: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Manually move a misfiled email into another bucket.

    If it becomes customer mail and has no draft yet, draft a reply on the spot.
    """
    approval = db.get(EmailApproval, approval_id)
    if not approval:
        return Response(status_code=404)
    approval.category = category
    approval.classification_reason = "Manually reclassified by staff"
    db.commit()

    if category == "customer" and approval.status == "pending" and not approval.draft_body:
        from app.services import cs_email_agent

        cs_email_agent.draft_reply(approval, db)

    db.refresh(approval)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_card.html",
        {"a": approval, "gmail_connected": _gmail_connected(db)},
    )


# ── Escalations ───────────────────────────────────────────────────────────────


@router.get("/escalations", response_class=HTMLResponse)
def cs_escalations(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    escalations = _get_escalations(db)
    return templates.TemplateResponse(
        request,
        "customer_service/_escalations.html",
        {"escalations": escalations, "pending_escalations": len(escalations)},
    )


@router.post("/escalations/dismiss-all", response_class=HTMLResponse)
def cs_escalation_dismiss_all(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _set_setting(db, "chatbot_escalation_pending", json.dumps([]))
    return templates.TemplateResponse(
        request,
        "customer_service/_escalations.html",
        {"escalations": [], "pending_escalations": 0},
    )


@router.post("/escalations/{index}/dismiss", response_class=HTMLResponse)
def cs_escalation_dismiss(
    index: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    escalations = _get_escalations(db)
    if 0 <= index < len(escalations):
        escalations.pop(index)
        _set_setting(db, "chatbot_escalation_pending", json.dumps(escalations))
    return templates.TemplateResponse(
        request,
        "customer_service/_escalations.html",
        {"escalations": escalations, "pending_escalations": len(escalations)},
    )


# ── Gmail OAuth ───────────────────────────────────────────────────────────────


@router.get("/gmail/connect")
def cs_gmail_connect(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    from app.services import gmail_monitor

    state = secrets.token_urlsafe(16)
    request.session["gmail_oauth_state"] = state
    redirect_uri = str(request.url_for("cs_gmail_callback"))
    auth_url, _ = gmail_monitor.build_gmail_auth_url(redirect_uri, state)
    return RedirectResponse(auth_url)


@router.get("/gmail/callback", name="cs_gmail_callback")
def cs_gmail_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error:
        return RedirectResponse(
            f"/customer-service/?flash_type=danger&flash_msg=Gmail+OAuth+error:+{error}"
        )

    saved_state = request.session.pop("gmail_oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse(
            "/customer-service/?flash_type=danger&flash_msg=OAuth+state+mismatch"
        )

    try:
        from app.services import gmail_monitor

        redirect_uri = str(request.url_for("cs_gmail_callback"))
        token_data = gmail_monitor.exchange_code_for_tokens(code, redirect_uri)
        gmail_monitor.store_tokens(db, token_data)
    except Exception as exc:
        return RedirectResponse(
            f"/customer-service/?flash_type=danger&flash_msg=Token+exchange+failed:+{exc}"
        )

    return RedirectResponse(
        "/customer-service/?flash_type=success&flash_msg=Gmail+inbox+connected+successfully!"
    )


@router.post("/gmail/poll", response_class=HTMLResponse)
def cs_gmail_poll(
    request: Request,
    status: str = "pending",
    category: str = "customer",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Manual 'Poll now' — runs synchronously and returns the refreshed queue."""
    from app.services import gmail_monitor

    if not _gmail_connected(db):
        return HTMLResponse(
            '<div class="alert alert-warning p-2 small mb-0">Gmail not connected.</div>'
        )

    poll_result: dict = {"count": 0, "error": None}
    try:
        created = gmail_monitor.poll_and_process(db)
        poll_result["count"] = len(created)
    except gmail_monitor.GmailReauthRequiredError as exc:
        # Token was just cleared by the service; surface the reconnect prompt
        # inline so the operator sees it without a full page reload.
        poll_result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — surface the error, still render the queue
        logger.warning("Manual Gmail poll failed: %s", exc)
        poll_result["error"] = f"Poll failed: {exc}"

    approvals = _query_email_queue(db, status, category)
    return templates.TemplateResponse(
        request,
        "customer_service/_email_queue.html",
        _email_queue_context(db, approvals, status, category, poll_result=poll_result),
    )


@router.post("/email-queue/autopoll", response_class=HTMLResponse)
def cs_email_autopoll(
    request: Request,
    enabled: str = Form("0"),
    interval: str = Form(_DEFAULT_POLL_INTERVAL),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Persist the auto-poll on/off toggle and interval (picked up by the loop)."""
    is_on = enabled in ("1", "true", "on", "True")
    _set_setting(db, "gmail_autopoll_enabled", "1" if is_on else "0")
    try:
        minutes = max(1, int(interval))
    except ValueError:
        minutes = int(_DEFAULT_POLL_INTERVAL)
    _set_setting(db, "gmail_poll_interval_minutes", str(minutes))

    state = f"on — checking every {minutes} min" if is_on else "off — manual polling only"
    return HTMLResponse(
        f'<span class="text-success small"><i class="bi bi-check-circle me-1"></i>'
        f"Auto-poll {state}.</span>"
    )


# ── Manager AI Chat ───────────────────────────────────────────────────────────


@router.post("/manager-chat", response_class=HTMLResponse)
def cs_manager_chat(
    request: Request,
    message: str = Form(...),
    user: dict = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    employee_email = user.get("email", "employee")
    session_id = f"manager:{employee_email}"

    # Save user message
    db.add(ChatMessage(session_id=session_id, role="user", content=message.strip()))
    db.commit()

    # Synchronous manager response
    reply = cs_manager_agent.run_manager_response(employee_email, message.strip(), db)

    # Save assistant response
    asst_msg = ChatMessage(session_id=session_id, role="assistant", content=reply)
    db.add(asst_msg)
    db.commit()
    db.refresh(asst_msg)

    # Return both bubbles as HTML
    user_html = (
        f'<div class="chat-msg chat-msg--user">'
        f'<div class="chat-bubble chat-bubble--user">{message.strip()}</div></div>'
    )
    asst_ts = asst_msg.created_at.strftime("%H:%M") if asst_msg.created_at else ""
    asst_html = (
        f'<div class="chat-msg chat-msg--assistant">'
        f'<div class="chat-bubble chat-bubble--assistant">{reply}</div>'
        f'<div class="chat-ts">{asst_ts}</div>'
        f"</div>"
    )
    return HTMLResponse(content=user_html + asst_html)


# ── Chat History ──────────────────────────────────────────────────────────────


def _parse_dt(v: object) -> datetime | None:
    """Coerce a value to datetime.

    SQLite aggregate functions return ISO strings, not datetime objects.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


def _build_chat_history_context(db: Session) -> dict:
    """Query public chatbot sessions and group them by calendar date."""
    sessions_raw = (
        db.query(
            ChatMessage.session_id,
            sql_func.count(ChatMessage.id).label("msg_count"),
            sql_func.max(ChatMessage.created_at).label("last_at"),
            sql_func.min(ChatMessage.created_at).label("started_at"),
        )
        .filter(~ChatMessage.session_id.like("manager:%"))
        .group_by(ChatMessage.session_id)
        .order_by(sql_func.max(ChatMessage.created_at).desc())
        .limit(100)
        .all()
    )

    sessions = []
    for row in sessions_raw:
        first_msg = (
            db.query(ChatMessage.content)
            .filter(
                ChatMessage.session_id == row.session_id,
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.id)
            .first()
        )
        sessions.append(
            {
                "session_id": row.session_id,
                "msg_count": row.msg_count,
                "last_at": _parse_dt(row.last_at),
                "started_at": _parse_dt(row.started_at),
                "first_message": first_msg[0][:80] if first_msg else "(no messages)",
            }
        )

    today = date_type.today()
    yesterday = today - timedelta(days=1)

    grouped: defaultdict = defaultdict(list)
    for s in sessions:
        day = s["last_at"].date() if s["last_at"] else today
        grouped[day].append(s)

    session_groups = [
        {"date": d, "sessions": grouped[d]} for d in sorted(grouped.keys(), reverse=True)
    ]

    return {
        "session_groups": session_groups,
        "total_sessions": len(sessions),
        "today": today,
        "yesterday": yesterday,
    }


@router.get("/chat-history", response_class=HTMLResponse)
def cs_chat_history(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "customer_service/_chat_history.html",
        _build_chat_history_context(db),
    )


# ── Chat History Delete ───────────────────────────────────────────────────────


@router.post("/chat-history/{session_id}/delete", response_class=HTMLResponse)
def cs_chat_history_delete_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete(
        synchronize_session=False
    )
    db.commit()
    return HTMLResponse("")


@router.post("/chat-history/delete-batch", response_class=HTMLResponse)
def cs_chat_history_delete_batch(
    request: Request,
    session_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Delete several public chatbot sessions at once. Manager chats are never touched."""
    safe_ids = [s for s in session_ids if s and not s.startswith("manager:")]
    if safe_ids:
        db.query(ChatMessage).filter(ChatMessage.session_id.in_(safe_ids)).delete(
            synchronize_session=False
        )
        db.commit()
    return templates.TemplateResponse(
        request,
        "customer_service/_chat_history.html",
        _build_chat_history_context(db),
    )


@router.post("/chat-history/clear", response_class=HTMLResponse)
def cs_chat_history_delete_all(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    db.query(ChatMessage).filter(ChatMessage.session_id.notlike("manager:%")).delete(
        synchronize_session=False
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "customer_service/_chat_history.html",
        {
            "session_groups": [],
            "total_sessions": 0,
            "today": date_type.today(),
            "yesterday": date_type.today(),
        },
    )


# ── AI Settings ───────────────────────────────────────────────────────────────


@router.get("/ai-settings", response_class=HTMLResponse)
def cs_ai_settings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.config import settings as app_settings
    from app.services.cs_chatbot_agent import PROVIDER_MODELS, get_active_provider

    current_provider, current_model = get_active_provider(db)

    key_status = [
        ("anthropic", "Claude", bool(app_settings.anthropic_api_key), "Paid"),
        ("groq", "Groq", bool(app_settings.groq_api_key), "Free tier"),
        ("openai", "OpenAI", bool(app_settings.openai_api_key), "Paid"),
        ("gemini", "Gemini", bool(app_settings.gemini_api_key), "Free tier"),
        ("ollama", "Ollama", _ollama_running(), "Local"),
    ]

    return templates.TemplateResponse(
        request,
        "customer_service/_ai_settings.html",
        {
            "providers": _PROVIDERS,
            "provider_models": PROVIDER_MODELS,
            "current_provider": current_provider,
            "current_model": current_model,
            "key_status": key_status,
        },
    )


@router.post("/ai-settings", response_class=HTMLResponse)
def cs_ai_settings_save(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _set_setting(db, "cs_ai_provider", provider)
    _set_setting(db, "cs_ai_model", model)
    return HTMLResponse(
        f'<span class="text-success"><i class="bi bi-check-circle me-1"></i>'
        f"Saved — chatbot now using <strong>{provider}</strong> / <strong>{model}</strong></span>"
    )


# ── Availability hours (chatbot consultation scheduling) ───────────────────────

# (weekday int, label) — Monday = 0 to match Python's date.weekday().
_WEEKDAYS: list[tuple[int, str]] = [
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
]

# (IANA name, friendly label) for the timezone picker.
_TIMEZONES: list[tuple[str, str]] = [
    ("America/New_York", "Eastern (New York)"),
    ("America/Chicago", "Central (Chicago)"),
    ("America/Denver", "Mountain (Denver)"),
    ("America/Phoenix", "Arizona (no DST)"),
    ("America/Los_Angeles", "Pacific (Los Angeles)"),
    ("America/Anchorage", "Alaska (Anchorage)"),
    ("Pacific/Honolulu", "Hawaii (Honolulu)"),
]

# Tunable global window fields shown alongside the per-day hours. (key, label, help, min, max)
_AVAIL_INT_FIELDS: list[tuple[str, str, str, int, int]] = [
    ("cal_slot_minutes", "Slot Length (min)", "Length of each consultation slot", 5, 480),
    ("cal_lookahead_days", "Days Ahead", "How many days out to offer times", 1, 60),
    ("cal_max_slots", "Max Times Shown", "Cap on how many openings to list at once", 1, 50),
]


def _hour_label(hour: int) -> str:
    """24h int -> friendly label, e.g. 0 -> '12:00 AM', 13 -> '1:00 PM'."""
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:00 {suffix}"


_HOUR_CHOICES: list[tuple[int, str]] = [(h, _hour_label(h)) for h in range(24)]


@router.get("/availability", response_class=HTMLResponse)
def cs_availability(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.services.google_calendar import booking_enabled, load_scheduling_config

    cfg = load_scheduling_config(db)
    day_rows = []
    for n, label in _WEEKDAYS:
        hours = cfg.day_hours.get(n)
        day_rows.append(
            {
                "n": n,
                "label": label,
                "enabled": hours is not None,
                "open": hours[0] if hours else 9,
                "close": hours[1] if hours else 17,
            }
        )
    int_values = {
        "cal_slot_minutes": cfg.slot_minutes,
        "cal_lookahead_days": cfg.lookahead_days,
        "cal_max_slots": cfg.max_slots,
    }
    return templates.TemplateResponse(
        request,
        "customer_service/_availability.html",
        {
            "day_rows": day_rows,
            "hour_choices": _HOUR_CHOICES,
            "int_fields": _AVAIL_INT_FIELDS,
            "int_values": int_values,
            "timezones": _TIMEZONES,
            "current_tz": str(cfg.tz),
            "booking_enabled": booking_enabled(db),
            "gmail_connected": _gmail_connected(db),
        },
    )


@router.post("/availability", response_class=HTMLResponse)
async def cs_availability_save(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    form = await request.form()

    # Per-day hours: keep only days that are checked AND have a valid open < close window.
    day_hours: dict[str, list[int]] = {}
    for n, _ in _WEEKDAYS:
        if f"day_{n}_enabled" not in form:
            continue
        try:
            open_h = int(str(form.get(f"day_{n}_open")))
            close_h = int(str(form.get(f"day_{n}_close")))
        except (TypeError, ValueError):
            continue
        if 0 <= open_h < close_h <= 23:
            day_hours[str(n)] = [open_h, close_h]
    _set_setting(db, "cal_day_hours", json.dumps(day_hours))

    # Global window integers, clamped to their documented ranges.
    for key, _, _, lo, hi in _AVAIL_INT_FIELDS:
        raw = str(form.get(key, "")).strip()
        if not raw:
            continue
        try:
            _set_setting(db, key, str(max(lo, min(hi, int(raw)))))
        except ValueError:
            logger.warning("Ignored non-int availability value %r=%r", key, raw)

    # Timezone (only accept a value from our known list).
    tz = str(form.get("cal_timezone", "")).strip()
    if tz in {name for name, _ in _TIMEZONES}:
        _set_setting(db, "cal_timezone", tz)

    open_days = len(day_hours)
    return HTMLResponse(
        f'<span class="text-success"><i class="bi bi-check-circle me-1"></i>'
        f"Saved — chatbot now offers times on <strong>{open_days}</strong> "
        f"day{'s' if open_days != 1 else ''} per week.</span>"
    )
