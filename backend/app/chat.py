"""Optional single-room chat backed by ntfy.

ntfy is deployed in-cluster and is never exposed to browsers; this module is
the only client that talks to it. Messages are published with the signed-in
user's name as the ntfy title, so the author shown in the UI comes from the
forward-auth proxy (oauth2-proxy), not from the message body.

Peppered through the same topic are a few *control* messages used to store
reactions, pins and stars. They are ordinary ntfy messages tagged with one of
``CONTROL_TAGS`` and are hidden from the feed, so ntfy stays the single source
of truth and no extra database is needed. Lifecycle events from the lab (VMs,
snapshots, images) are published as :data:`SYSTEM_AUTHOR` messages.

Disabled unless CHAT_ENABLED=true and CHAT_NTFY_URL are set.
"""
import json
import logging
from datetime import datetime

import httpx
import markdown
import nh3
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.auth import current_user
from app.config import settings
from app.ui import templates

logger = logging.getLogger("labforge.chat")

router = APIRouter(tags=["chat"])

# Author used for lab lifecycle events ("web1 started").
SYSTEM_AUTHOR = "labforge"

# Tags marking control messages (hidden from the feed).
REACTION_TAG = "labforge-reaction"
PIN_TAG = "labforge-pin"
STAR_TAG = "labforge-star"
CONTROL_TAGS = {REACTION_TAG, PIN_TAG, STAR_TAG}

# Emoji offered in the reaction picker.
REACTION_EMOJIS = ["👍", "❤️", "😂", "🎉", "🚀"]

_MAX_MESSAGES = 500          # newest messages kept in the rendered feed
_GROUP_WINDOW_SECONDS = 300  # consecutive same-author messages grouped within 5m
_MAX_TEXT = 2000

# --- Markdown -> sanitised HTML --------------------------------------------
_MD_EXTENSIONS = ["extra", "pymdownx.magiclink", "sane_lists", "nl2br"]
_HTML_TAGS = {
    "p", "br", "strong", "em", "del", "code", "pre", "blockquote", "hr",
    "ul", "ol", "li", "a", "img", "h1", "h2", "h3", "h4",
    "table", "thead", "tbody", "tr", "th", "td",
}
_HTML_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "code": {"class"},
}


def render_markdown(text: str) -> str:
    """Render user text to safe HTML: Markdown plus bare-URL autolinking."""
    if not text:
        return ""
    html = markdown.markdown(text, extensions=_MD_EXTENSIONS)
    return nh3.clean(
        html,
        tags=_HTML_TAGS,
        attributes=_HTML_ATTRS,
        url_schemes={"http", "https", "mailto"},
        url_relative="deny",
        link_rel="noopener noreferrer",
    )


# --- ntfy plumbing ----------------------------------------------------------

def enabled() -> bool:
    """Chat is on only when explicitly enabled and a backend URL is configured."""
    return bool(settings.chat_enabled and settings.chat_ntfy_url)


def _base() -> str:
    return (settings.chat_ntfy_url or "").rstrip("/")


def _topic() -> str:
    return settings.chat_topic or "labforge"


def _parse_events(text: str) -> list[dict]:
    """Parse ntfy's newline-delimited JSON into a list of message objects."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def fetch_raw() -> list[dict]:
    """Return every cached event in the topic (control messages included)."""
    if not enabled():
        return []
    try:
        resp = httpx.get(f"{_base()}/{_topic()}/json", params={"poll": "1"}, timeout=5.0)
        resp.raise_for_status()
        return _parse_events(resp.text)
    except httpx.HTTPError as exc:
        logger.warning("chat history fetch failed: %s", exc)
        return []


def publish(author: str, text: str, *, tags: list[str] | None = None,
            click: str | None = None) -> None:
    """Publish a message. Raises on transport/HTTP failure."""
    body: dict = {"topic": _topic(), "message": text, "title": author}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
    resp = httpx.post(f"{_base()}/", json=body, timeout=5.0)
    resp.raise_for_status()


def notify_system(text: str, *, tags: list[str] | None = None,
                  click: str | None = None) -> None:
    """Best-effort lifecyle event into the room; never raises."""
    if not enabled():
        return
    try:
        publish(SYSTEM_AUTHOR, text, tags=tags, click=click)
    except (httpx.HTTPError, ValueError) as exc:  # noqa: BLE001
        logger.warning("chat system publish failed: %s", exc)


def delete_message(message_id: str) -> None:
    """Delete a single cached message by id (ntfy ``DELETE /topic/id``)."""
    resp = httpx.delete(f"{_base()}/{_topic()}/{message_id}", timeout=5.0)
    resp.raise_for_status()


def _sse_event(obj: dict) -> str | None:
    """Convert one ntfy event into an SSE chunk, or None if it is not forwarded."""
    event = obj.get("event")
    if event == "keepalive":
        return ": keepalive\n\n"
    if event not in ("message", "message_delete", "message_clear"):
        return None
    return f"id: {obj.get('id', '')}\nevent: chat\ndata: {json.dumps(obj)}\n\n"


# --- state ------------------------------------------------------------------

def _control_body(tag: str, message: dict) -> dict | str | None:
    payload = message.get("message") or ""
    if tag == REACTION_TAG:
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return payload.strip()


def _live_events(raw: list[dict]) -> list[dict]:
    """Return message events that are still in effect.

    ntfy keeps deleted messages in the cache and announces a deletion with a
    separate ``message_delete`` event whose ``sequence_id`` points at the
    deleted message (its own ``id`` is the event id). ``message_clear`` wipes
    everything published before it. Control messages (reactions/pins/stars) are
    tombstoned the same way, so callers use this to skip deleted events.
    """
    events = sorted(raw, key=lambda x: x.get("time") or 0)
    clear_time: int | None = None
    deleted: set = set()
    for m in events:
        event = m.get("event")
        if event == "message_clear":
            clear_time = m.get("time") or 0
            deleted = set()
        elif event == "message_delete":
            target = m.get("sequence_id") or m.get("id")
            if target:
                deleted.add(target)

    live: list[dict] = []
    for m in events:
        if m.get("event") != "message":
            continue
        if clear_time is not None and (m.get("time") or 0) <= clear_time:
            continue
        if m.get("id") in deleted or m.get("sequence_id") in deleted:
            continue
        live.append(m)
    return live


def build_state(raw: list[dict]) -> tuple[dict, dict, dict, dict]:
    """Fold raw ntfy events into (visible, reactions, pins, stars).

    ``visible`` maps message id -> raw message. ``reactions`` maps a target id
    to ``{emoji: {users}}``; ``pins``/``stars`` map a target id to a set of
    users who pinned/starred it.
    """
    visible: dict[str, dict] = {}
    reactions: dict[str, dict[str, set]] = {}
    pins: dict[str, set] = {}
    stars: dict[str, set] = {}

    for m in _live_events(raw):
        mid = m.get("id")
        author = (m.get("title") or "")[:64]
        tags = set(m.get("tags") or [])
        if REACTION_TAG in tags:
            body = _control_body(REACTION_TAG, m)
            if isinstance(body, dict) and body.get("target") and body.get("emoji"):
                by_emoji = reactions.setdefault(body["target"], {})
                by_emoji.setdefault(str(body["emoji"]), set()).add(author)
            continue
        if PIN_TAG in tags:
            target = _control_body(PIN_TAG, m)
            if target:
                pins.setdefault(target, set()).add(author)
            continue
        if STAR_TAG in tags:
            target = _control_body(STAR_TAG, m)
            if target:
                stars.setdefault(target, set()).add(author)
            continue

        if mid:
            visible[mid] = m

    return visible, reactions, pins, stars


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _day_label(ts: int) -> str:
    day = _day_key(ts)
    today = datetime.now().strftime("%Y-%m-%d")
    if day == today:
        return "Today"
    return datetime.fromtimestamp(ts).strftime("%a %d %b")


def _time_label(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _author_hue(author: str) -> int:
    """Stable 0-359 hue for an author's initials avatar."""
    h = 0
    for ch in author:
        h = (h * 31 + ord(ch)) & 0xFFFFFF
    return h % 360


def _message_out(mid: str, m: dict, user: str,
                 reactions: dict, pins: dict, stars: dict) -> dict:
    author = (m.get("title") or "lab")[:64]
    rx = reactions.get(mid, {})
    return {
        "id": mid,
        "author": author,
        "initial": (author[:1] or "?").upper(),
        "hue": _author_hue(author),
        "system": author == SYSTEM_AUTHOR,
        "text": m.get("message") or "",
        "html": render_markdown(m.get("message") or ""),
        "time": int(m.get("time") or 0),
        "time_str": _time_label(m.get("time") or 0),
        "tags": [t for t in (m.get("tags") or []) if t not in CONTROL_TAGS],
        "click": m.get("click") or "",
        "mine": author == user,
        "pinned": mid in pins,
        "starred": mid in stars,
        "reactions": [
            {"emoji": emoji, "count": len(users), "mine": user in users}
            for emoji, users in sorted(rx.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
    }


def chat_model(user: str) -> dict:
    """Everything the chat partial needs, rendered server-side."""
    visible, reactions, pins, stars = build_state(fetch_raw())
    ordered = sorted(visible.items(), key=lambda kv: kv[1].get("time") or 0)
    messages = [_message_out(mid, m, user, reactions, pins, stars)
                for mid, m in ordered][-_MAX_MESSAGES:]

    items: list[dict] = []
    prev: dict | None = None
    prev_day: str | None = None
    for msg in messages:
        day = _day_key(msg["time"])
        if day != prev_day:
            items.append({"kind": "day", "time": msg["time"], "label": _day_label(msg["time"])})
            prev_day = day
            prev = None
        grouped = (
            prev is not None
            and not msg["system"] and not prev["system"]
            and msg["author"] == prev["author"]
            and (msg["time"] - prev["time"]) <= _GROUP_WINDOW_SECONDS
        )
        items.append({"kind": "msg", **msg, "grouped": grouped})
        prev = msg

    pinned = [m for m in messages if m["pinned"]]
    return {
        "items": items,
        "pinned": pinned,
        "chat_topic": _topic(),
        "current_user": user,
        "reaction_emojis": REACTION_EMOJIS,
    }


# --- control toggle helpers -------------------------------------------------

def _find_control(raw: list[dict], tag: str, target: str, user: str,
                  emoji: str | None = None) -> dict | None:
    for m in _live_events(raw):
        if tag not in set(m.get("tags") or []):
            continue
        if (m.get("title") or "") != user:
            continue
        body = _control_body(tag, m)
        if tag == REACTION_TAG:
            if isinstance(body, dict) and body.get("target") == target \
                    and (emoji is None or body.get("emoji") == emoji):
                return m
        elif body == target:
            return m
    return None


def toggle_reaction(user: str, target: str, emoji: str) -> None:
    if not target or not emoji:
        return
    raw = fetch_raw()
    existing = _find_control(raw, REACTION_TAG, target, user, emoji=emoji)
    if existing and existing.get("id"):
        delete_message(existing["id"])
    else:
        publish(user, json.dumps({"target": target, "emoji": emoji}), tags=[REACTION_TAG])


def _toggle_flag(tag: str, user: str, target: str) -> None:
    if not target:
        return
    raw = fetch_raw()
    existing = _find_control(raw, tag, target, user)
    if existing and existing.get("id"):
        delete_message(existing["id"])
    else:
        publish(user, target, tags=[tag])


def toggle_pin(user: str, target: str) -> None:
    _toggle_flag(PIN_TAG, user, target)


def toggle_star(user: str, target: str) -> None:
    _toggle_flag(STAR_TAG, user, target)


def delete_own(user: str, target: str) -> None:
    """Delete a message only if the signed-in user authored it."""
    if not target:
        return
    for m in fetch_raw():
        if m.get("id") == target and m.get("event", "message") == "message":
            if set(m.get("tags") or []) & CONTROL_TAGS:
                raise HTTPException(status_code=403, detail="Not a chat message")
            if (m.get("title") or "") != user:
                raise HTTPException(status_code=403, detail="You can only delete your own messages")
            delete_message(target)
            return
    # Already gone: treat as success.
    return


# --- routes -----------------------------------------------------------------

def _partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/chat_messages.html", chat_model(current_user(request))
    )


@router.get("/partials/chat/messages", response_class=HTMLResponse)
def chat_messages(request: Request):
    return _partial(request)


@router.post("/chat/send", response_class=HTMLResponse)
def chat_send(request: Request, message: str = Form(default="")):
    text = (message or "").strip()[:_MAX_TEXT]
    if text and enabled():
        try:
            publish(current_user(request), text)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chat publish failed: %s", exc)
    return _partial(request)


@router.post("/chat/react", response_class=HTMLResponse)
def chat_react(request: Request, id: str = Form(default=""), emoji: str = Form(default="")):
    if enabled() and emoji in REACTION_EMOJIS:
        try:
            toggle_reaction(current_user(request), id, emoji)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chat reaction failed: %s", exc)
    return _partial(request)


@router.post("/chat/pin", response_class=HTMLResponse)
def chat_pin(request: Request, id: str = Form(default="")):
    if enabled():
        try:
            toggle_pin(current_user(request), id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chat pin failed: %s", exc)
    return _partial(request)


@router.post("/chat/star", response_class=HTMLResponse)
def chat_star(request: Request, id: str = Form(default="")):
    if enabled():
        try:
            toggle_star(current_user(request), id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chat star failed: %s", exc)
    return _partial(request)


@router.post("/chat/delete", response_class=HTMLResponse)
def chat_delete(request: Request, id: str = Form(default="")):
    if enabled():
        try:
            delete_own(current_user(request), id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chat delete failed: %s", exc)
    return _partial(request)


@router.get("/chat/stream")
async def chat_stream(request: Request):
    """Server-Sent Events: notify the browser whenever the topic changes.

    The browser re-fetches the rendered partial on each event, so this stream
    only needs to signal *that* something changed (and resume from
    ``Last-Event-ID`` after a reconnect).
    """
    if not enabled():
        raise HTTPException(status_code=404, detail="Chat is disabled")

    last_id = request.headers.get("last-event-id") or ""
    params = {"since": last_id} if last_id else None

    async def event_source():
        yield ": connected\n\n"
        timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "GET", f"{_base()}/{_topic()}/json", params=params
                ) as resp:
                    async for line in resp.aiter_lines():
                        if await request.is_disconnected():
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        chunk = _sse_event(obj)
                        if chunk is not None:
                            yield chunk
        except httpx.HTTPError as exc:
            logger.warning("chat stream failed: %s", exc)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
