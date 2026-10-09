"""Optional single-room chat backed by ntfy.

ntfy is deployed in-cluster and is never exposed to browsers; this module is
the only client. Messages are published with the signed-in user's name as the
ntfy title, so the author shown in the UI comes from the forward-auth proxy
(oauth2-proxy), not from the message body.

Disabled unless CHAT_ENABLED=true and CHAT_NTFY_URL are set.
"""
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user
from app.config import settings
from app.ui import templates

logger = logging.getLogger("labforge.chat")

router = APIRouter(tags=["chat"])

_MAX_MESSAGES = 200


def enabled() -> bool:
    """Chat is on only when explicitly enabled and a backend URL is configured."""
    return bool(settings.chat_enabled and settings.chat_ntfy_url)


def _base() -> str:
    return (settings.chat_ntfy_url or "").rstrip("/")


def _topic() -> str:
    return settings.chat_topic or "labforge"


def _format_time(epoch: int | float | None) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def fetch_messages() -> list[dict]:
    """Return recent cached messages, oldest first."""
    if not enabled():
        return []
    try:
        resp = httpx.get(f"{_base()}/{_topic()}/json", params={"poll": "1"}, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("chat history fetch failed: %s", exc)
        return []
    messages = [m for m in data if m.get("event", "message") == "message"]
    messages.sort(key=lambda m: m.get("time") or 0)
    return [
        {
            "author": (m.get("title") or "lab")[:64],
            "text": m.get("message") or "",
            "time": _format_time(m.get("time")),
        }
        for m in messages[-_MAX_MESSAGES:]
    ]


def publish(author: str, text: str) -> None:
    """Publish a message. Raises on transport/HTTP failure."""
    resp = httpx.post(
        f"{_base()}/",
        json={"topic": _topic(), "message": text, "title": author},
        timeout=5.0,
    )
    resp.raise_for_status()


def _context() -> dict:
    return {"messages": fetch_messages(), "chat_topic": _topic()}


@router.get("/partials/chat/messages", response_class=HTMLResponse)
def chat_messages(request: Request):
    return templates.TemplateResponse(request, "partials/chat_messages.html", _context())


@router.post("/chat/send", response_class=HTMLResponse)
def chat_send(request: Request, message: str = Form(default="")):
    text = (message or "").strip()[:2000]
    if text and enabled():
        try:
            publish(current_user(request), text)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chat publish failed: %s", exc)
    return templates.TemplateResponse(request, "partials/chat_messages.html", _context())
