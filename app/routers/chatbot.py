"""Public chatbot endpoints — no authentication required."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.chat import ChatMessage
from app.services import cs_chatbot_agent
from app.views import templates

router = APIRouter(prefix="/chatbot", tags=["chatbot"])

_COOKIE_NAME = "chatbot_session"
_COOKIE_MAX_AGE = 86400 * 30  # 30 days


@router.post("/message", response_class=HTMLResponse)
def chatbot_message(
    request: Request,
    message: str = Form(...),
    session_id: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not session_id or session_id == "new":
        session_id = str(uuid.uuid4())

    # Save user message first so it appears in conversation history
    user_msg = ChatMessage(session_id=session_id, role="user", content=message.strip())
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # Call AI synchronously — FastAPI runs sync handlers in a thread pool.
    # get_chatbot_reply persists the assistant message itself; we re-fetch it below.
    cs_chatbot_agent.get_chatbot_reply(session_id, message.strip(), db, before_id=user_msg.id)

    # Fetch the assistant message that get_chatbot_reply just saved
    assistant_msg = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.id.desc())
        .first()
    )

    # Return only the assistant bubble — user bubble is shown instantly via JS
    html = templates.TemplateResponse(
        request,
        "chatbot/_message.html",
        {
            "msg": assistant_msg,
            "session_id": session_id,
            "last_id": assistant_msg.id if assistant_msg else 0,
            "show_poll": False,
        },
    )
    html.set_cookie(
        _COOKIE_NAME,
        session_id,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return html


@router.post("/lead", response_class=HTMLResponse)
def chatbot_submit_lead(
    request: Request,
    session_id: str = Form(""),
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not name.strip():
        return HTMLResponse(
            '<p style="color:#dc3545;font-size:.75rem;margin:4px 0 6px;">'
            "Please enter your name.</p>",
            status_code=422,
        )
    from app.services.cs_chatbot_agent import _handle_capture_lead

    _handle_capture_lead(
        {
            "name": name,
            "email": email,
            "phone": phone,
            "location": location,
            "description": description,
        },
        session_id.strip() or "widget-form",
        db,
    )
    return HTMLResponse(
        '<div style="text-align:center;padding:12px 0;">'
        '<div style="font-size:2rem;margin-bottom:6px;">✅</div>'
        '<p style="font-weight:600;color:#198754;margin-bottom:4px;font-size:.88rem;">'
        "Request received!</p>"
        '<p style="font-size:.75rem;color:#666;margin-bottom:10px;">'
        "A real team member will personally reach out soon.</p>"
        '<button type="button" onclick="hideCallbackForm()" '
        'style="background:none;border:1px solid #dee2e6;border-radius:6px;'
        'padding:4px 14px;font-size:.78rem;cursor:pointer;color:#6c757d;">'
        "Back to Chat</button>"
        "</div>"
    )


@router.get("/history/{session_id}", response_class=HTMLResponse)
def chatbot_session_history(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    msgs = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(["user", "assistant"]),
        )
        .order_by(ChatMessage.id)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "chatbot/_history_detail.html",
        {"msgs": msgs, "session_id": session_id},
    )
