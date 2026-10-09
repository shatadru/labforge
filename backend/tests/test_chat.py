"""Tests for the optional ntfy-backed chat card.

The network layer is stubbed with an in-memory ntfy; nothing here touches a
real server.
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import chat
from app.main import app


class FakeResponse:
    def __init__(self, payload=None, status=200, text=None):
        self._payload = payload
        self.status_code = status
        if text is not None:
            self.text = text
        elif payload is None:
            self.text = ""
        else:
            self.text = "\n".join(json.dumps(item) for item in payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class FakeNtfy:
    """A tiny in-memory ntfy: messages persist and control tags behave."""

    def __init__(self):
        self.messages: list[dict] = []
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"id{self._seq}"

    def get(self, url, params=None, timeout=None):
        return FakeResponse(text="\n".join(json.dumps(m) for m in self.messages))

    def post(self, url, json=None, timeout=None):
        msg = {"id": self._next_id(), "time": 1000 + self._seq, "event": "message"}
        msg.update(json or {})
        self.messages.append(msg)
        return FakeResponse(status=200)

    def delete(self, url, timeout=None):
        # ntfy keeps the message and announces a deletion with a separate
        # message_delete event whose sequence_id points at the deleted message.
        mid = url.rstrip("/").rsplit("/", 1)[-1]
        self.messages.append({
            "id": self._next_id(),
            "time": 1000 + self._seq,
            "event": "message_delete",
            "sequence_id": mid,
        })
        return FakeResponse(status=200)


@pytest.fixture
def web():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def chat_on(monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_enabled", True)
    monkeypatch.setattr(chat.settings, "chat_ntfy_url", "http://ntfy.labforge.svc:80")
    monkeypatch.setattr(chat.settings, "chat_topic", "labforge")
    monkeypatch.setattr(chat.settings, "auth_user_header", "X-Forwarded-User")


@pytest.fixture
def ntfy(monkeypatch):
    fake = FakeNtfy()
    monkeypatch.setattr(chat.httpx, "get", fake.get)
    monkeypatch.setattr(chat.httpx, "post", fake.post)
    monkeypatch.setattr(chat.httpx, "delete", fake.delete)
    return fake


# ---------------------------------------------------------------- helpers

def test_enabled_requires_url(monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_enabled", True)
    monkeypatch.setattr(chat.settings, "chat_ntfy_url", None)
    assert chat.enabled() is False
    monkeypatch.setattr(chat.settings, "chat_ntfy_url", "http://ntfy:80")
    assert chat.enabled() is True


def test_current_user_reexport(monkeypatch):
    monkeypatch.setattr(chat.settings, "auth_user_header", "X-Forwarded-User")
    assert chat.current_user(SimpleNamespace(headers={"X-Forwarded-User": "alice"})) == "alice"


# ---------------------------------------------------------------- rendering

def test_render_markdown_formats_and_linkifies():
    html = chat.render_markdown("**bold** and https://example.com/x")
    assert "<strong>bold</strong>" in html
    assert '<a href="https://example.com/x"' in html


def test_render_markdown_sanitises():
    html = chat.render_markdown("<script>alert(1)</script> [x](javascript:alert(1))")
    assert "<script>" not in html
    assert "javascript:" not in html


def test_render_markdown_empty():
    assert chat.render_markdown("") == ""


def test_render_markdown_hardens_images():
    html = chat.render_markdown("![shot](https://example.com/x.png)")
    assert 'loading="lazy"' in html
    assert 'referrerpolicy="no-referrer"' in html
    assert "<img" in html


def test_render_markdown_is_cached(monkeypatch):
    # Calls with the same text must hit the cache after the first.
    chat.render_markdown.cache_clear()
    chat.render_markdown("**cached**")
    info_before = chat.render_markdown.cache_info()
    chat.render_markdown("**cached**")
    info_after = chat.render_markdown.cache_info()
    assert info_after.hits == info_before.hits + 1


# ---------------------------------------------------------------- parsing

def test_parse_events_handles_ndjson():
    text = '{"id":"a"}\n\nnot-json\n{"id":"b"}\n'
    events = chat._parse_events(text)
    assert [e["id"] for e in events] == ["a", "b"]


def test_fetch_raw_disabled(monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_enabled", False)
    assert chat.fetch_raw() == []


def test_fetch_raw_survives_http_error(chat_on, monkeypatch):
    def boom(*a, **k):
        raise httpx.HTTPError("nope")
    monkeypatch.setattr(chat.httpx, "get", boom)
    assert chat.fetch_raw() == []


def test_fetch_raw_parses_single_message(chat_on, ntfy):
    ntfy.messages = [{"id": "m1", "time": 5, "event": "message", "title": "alice", "message": "hi"}]
    raw = chat.fetch_raw()
    assert raw[0]["message"] == "hi"


# ---------------------------------------------------------------- state

def _msg(mid, author, text, time=1, tags=None):
    m = {"id": mid, "time": time, "event": "message", "title": author, "message": text}
    if tags:
        m["tags"] = tags
    return m


def test_build_state_aggregates_controls_and_hides_them():
    raw = [
        _msg("m1", "alice", "hello"),
        _msg("r1", "bob", json.dumps({"target": "m1", "emoji": "👍"}), tags=[chat.REACTION_TAG]),
        _msg("p1", "bob", "m1", tags=[chat.PIN_TAG]),
        _msg("s1", "alice", "m1", tags=[chat.STAR_TAG]),
    ]
    visible, reactions, pins, stars = chat.build_state(raw)
    assert set(visible) == {"m1"}
    assert reactions["m1"]["👍"] == {"bob"}
    assert pins["m1"] == {"bob"}
    assert stars["m1"] == {"alice"}


def test_build_state_handles_delete_and_clear():
    raw = [
        _msg("m1", "alice", "one", time=1),
        _msg("m2", "alice", "two", time=2),
        {"id": "m1", "event": "message_delete", "time": 3},
    ]
    visible, _, _, _ = chat.build_state(raw)
    assert set(visible) == {"m2"}
    visible, _, _, _ = chat.build_state(raw + [{"event": "message_clear", "time": 4}])
    assert visible == {}


def test_build_state_uses_delete_sequence_id():
    # ntfy's delete event carries its own id and the target in sequence_id.
    raw = [
        _msg("m1", "alice", "hi", time=1),
        _msg("m2", "alice", "keep", time=2),
        {"id": "evt1", "event": "message_delete", "sequence_id": "m1", "time": 3},
    ]
    visible, _, _, _ = chat.build_state(raw)
    assert set(visible) == {"m2"}


def test_build_state_hides_deleted_reaction():
    raw = [
        _msg("m1", "alice", "hi", time=1),
        _msg("r1", "bob", json.dumps({"target": "m1", "emoji": "👍"}),
             time=2, tags=[chat.REACTION_TAG]),
        {"id": "evt1", "event": "message_delete", "sequence_id": "r1", "time": 3},
    ]
    _, reactions, _, _ = chat.build_state(raw)
    assert reactions.get("m1", {}) == {}


def test_chat_model_groups_and_separates_days():
    raw = [
        _msg("m1", "alice", "one", time=100000),
        _msg("m2", "alice", "two", time=100030),   # grouped, same author, +30s
        _msg("m3", "bob", "three", time=100060),   # new author
        _msg("m4", "bob", "four", time=100060 + 86400),  # next day
    ]
    ntfy_state = raw
    import app.chat as c
    orig = c.fetch_raw
    c.fetch_raw = lambda: ntfy_state
    try:
        model = c.chat_model("alice")
    finally:
        c.fetch_raw = orig
    kinds = [i["kind"] for i in model["items"]]
    assert kinds.count("day") == 2
    msgs = [i for i in model["items"] if i["kind"] == "msg"]
    assert msgs[0]["mine"] is True
    assert msgs[1]["grouped"] is True
    assert msgs[2]["grouped"] is False


def test_chat_model_pins_and_reactions(chat_on, ntfy):
    ntfy.messages = [
        _msg("m1", "alice", "hello", time=1),
        _msg("p1", "bob", "m1", time=2, tags=[chat.PIN_TAG]),
        _msg("r1", "bob", json.dumps({"target": "m1", "emoji": "🎉"}), time=3, tags=[chat.REACTION_TAG]),
    ]
    model = chat.chat_model("bob")
    assert model["pinned"][0]["id"] == "m1"
    msg = [i for i in model["items"] if i["kind"] == "msg"][0]
    assert msg["reactions"] == [{"emoji": "🎉", "count": 1, "mine": True}]


# ---------------------------------------------------------------- controls

def test_toggle_reaction_adds_then_removes(chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    chat.toggle_reaction("bob", "m1", "👍")
    _, reactions, _, _ = chat.build_state(ntfy.messages)
    assert reactions["m1"]["👍"] == {"bob"}
    chat.toggle_reaction("bob", "m1", "👍")
    _, reactions, _, _ = chat.build_state(ntfy.messages)
    assert reactions.get("m1", {}) == {}
    # Deleting is a tombstone, so toggling on again re-adds the reaction.
    chat.toggle_reaction("bob", "m1", "👍")
    _, reactions, _, _ = chat.build_state(ntfy.messages)
    assert reactions["m1"]["👍"] == {"bob"}


def test_toggle_reaction_ignores_blank(chat_on, ntfy):
    chat.toggle_reaction("bob", "", "")
    assert ntfy.messages == []


def test_toggle_pin_and_star(chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    chat.toggle_pin("bob", "m1")
    chat.toggle_star("bob", "m1")
    _, _, pins, stars = chat.build_state(ntfy.messages)
    assert pins["m1"] == {"bob"}
    assert stars["m1"] == {"bob"}
    # Toggle off then on again.
    chat.toggle_pin("bob", "m1")
    _, _, pins, _ = chat.build_state(ntfy.messages)
    assert pins.get("m1", set()) == set()


def test_delete_own_enforces_ownership(chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    with pytest.raises(chat.HTTPException):
        chat.delete_own("bob", "m1")
    visible, _, _, _ = chat.build_state(ntfy.messages)
    assert "m1" in visible
    chat.delete_own("alice", "m1")
    visible, _, _, _ = chat.build_state(ntfy.messages)
    assert "m1" not in visible


def test_delete_own_rejects_control_and_missing(chat_on, ntfy):
    ntfy.messages = [_msg("r1", "alice", "m1", time=1, tags=[chat.REACTION_TAG])]
    with pytest.raises(chat.HTTPException):
        chat.delete_own("alice", "r1")
    chat.delete_own("alice", "does-not-exist")  # no-op, no raise


def test_notify_system_best_effort(chat_on, ntfy):
    chat.notify_system("created web1", tags=["🚀"], click="/vms/web1")
    assert ntfy.messages[-1]["title"] == chat.SYSTEM_AUTHOR
    assert ntfy.messages[-1]["message"] == "created web1"


def test_notify_system_disabled_is_noop(monkeypatch, ntfy):
    monkeypatch.setattr(chat.settings, "chat_enabled", False)
    chat.notify_system("nope")
    assert ntfy.messages == []


def test_sse_event():
    assert chat._sse_event({"event": "keepalive"}) == ": keepalive\n\n"
    assert chat._sse_event({"event": "open"}) is None
    chunk = chat._sse_event({"event": "message", "id": "m1"})
    assert chunk.startswith("id: m1\nevent: chat\ndata: ")


# ---------------------------------------------------------------- routes

def test_partial_renders_messages(web, chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hello **world**", time=1)]
    resp = web.get("/partials/chat/messages", headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 200
    assert "alice" in resp.text
    assert "<strong>world</strong>" in resp.text


def test_send_publishes_and_rerenders(web, chat_on, ntfy):
    resp = web.post("/chat/send", data={"message": "hi there"},
                    headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 200
    assert ntfy.messages[-1]["title"] == "alice"
    assert "hi there" in resp.text


def test_send_ignores_blank(web, chat_on, ntfy):
    resp = web.post("/chat/send", data={"message": "   "},
                    headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 200
    assert ntfy.messages == []


def test_send_rejects_oversized_bytes(web, chat_on, ntfy):
    # 2000 chars of emoji exceed the 4000-byte cap ntfy imposes.
    resp = web.post("/chat/send", data={"message": "💥" * 2000},
                    headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 413
    assert ntfy.messages == []


def test_send_returns_502_on_backend_failure(web, chat_on, ntfy, monkeypatch):
    def boom(*a, **k):
        raise httpx.HTTPError("down")
    monkeypatch.setattr(chat.httpx, "post", boom)
    resp = web.post("/chat/send", data={"message": "hi"},
                    headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 502


def test_react_returns_502_on_backend_failure(web, chat_on, ntfy, monkeypatch):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]

    def boom(url, json=None, timeout=None):
        raise httpx.HTTPError("down")
    monkeypatch.setattr(chat.httpx, "post", boom)
    resp = web.post("/chat/react", data={"id": "m1", "emoji": "👍"},
                    headers={"X-Forwarded-User": "bob"})
    assert resp.status_code == 502


def test_react_route_toggles(web, chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    resp = web.post("/chat/react", data={"id": "m1", "emoji": "👍"},
                    headers={"X-Forwarded-User": "bob"})
    assert resp.status_code == 200
    assert "👍" in resp.text
    _, reactions, _, _ = chat.build_state(ntfy.messages)
    assert reactions["m1"]["👍"] == {"bob"}


def test_react_route_rejects_unknown_emoji(web, chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    web.post("/chat/react", data={"id": "m1", "emoji": "💥"},
             headers={"X-Forwarded-User": "bob"})
    assert not any(chat.REACTION_TAG in (m.get("tags") or []) for m in ntfy.messages)


def test_pin_and_star_routes(web, chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    web.post("/chat/pin", data={"id": "m1"}, headers={"X-Forwarded-User": "bob"})
    web.post("/chat/star", data={"id": "m1"}, headers={"X-Forwarded-User": "bob"})
    _, _, pins, stars = chat.build_state(ntfy.messages)
    assert pins["m1"] == {"bob"}
    assert stars["m1"] == {"bob"}


def test_delete_route_forbidden_for_others(web, chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    resp = web.post("/chat/delete", data={"id": "m1"}, headers={"X-Forwarded-User": "bob"})
    assert resp.status_code == 403
    visible, _, _, _ = chat.build_state(ntfy.messages)
    assert "m1" in visible


def test_delete_route_removes_own(web, chat_on, ntfy):
    ntfy.messages = [_msg("m1", "alice", "hi", time=1)]
    resp = web.post("/chat/delete", data={"id": "m1"}, headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 200
    assert 'data-id="m1"' not in resp.text


def test_stream_disabled_404(web, monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_enabled", False)
    assert web.get("/chat/stream").status_code == 404
