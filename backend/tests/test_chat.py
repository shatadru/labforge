"""Tests for the optional ntfy-backed chat card.

The network layer is stubbed; nothing here touches a real ntfy.
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
            # ntfy serves newline-delimited JSON, one object per line.
            self.text = "\n".join(json.dumps(item) for item in payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


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


# ---------------------------------------------------------------- helpers

def test_enabled_requires_url(monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_enabled", True)
    monkeypatch.setattr(chat.settings, "chat_ntfy_url", None)
    assert chat.enabled() is False
    monkeypatch.setattr(chat.settings, "chat_ntfy_url", "http://ntfy:80")
    assert chat.enabled() is True


def test_current_user_prefers_forwarded_header(monkeypatch):
    monkeypatch.setattr(chat.settings, "auth_user_header", "X-Forwarded-User")
    req = SimpleNamespace(headers={"X-Forwarded-User": "alice"})
    assert chat.current_user(req) == "alice"
    assert chat.current_user(SimpleNamespace(headers={})) == "local"


def test_format_time_handles_bad_input():
    assert chat._format_time(None) == ""
    assert chat._format_time("not-a-time") == ""


def test_fetch_messages_sorts_filters_and_labels(chat_on, monkeypatch):
    payload = [
        {"event": "message", "time": 200, "title": "bob", "message": "second"},
        {"event": "message", "time": 100, "title": "alice", "message": "first"},
        {"event": "open", "time": 50},
    ]
    monkeypatch.setattr(chat.httpx, "get", lambda *a, **k: FakeResponse(payload))

    messages = chat.fetch_messages()
    assert [m["author"] for m in messages] == ["alice", "bob"]
    assert [m["text"] for m in messages] == ["first", "second"]


def test_fetch_messages_parses_single_ndjson_message(chat_on, monkeypatch):
    # Regression: a single cached message used to be parsed as a dict, so
    # `for m in data` iterated the dict's keys and raised AttributeError.
    body = '{"event":"message","time":300,"title":"alice","message":"only"}\n'
    monkeypatch.setattr(chat.httpx, "get", lambda *a, **k: FakeResponse(text=body))
    messages = chat.fetch_messages()
    assert [m["text"] for m in messages] == ["only"]
    assert [m["author"] for m in messages] == ["alice"]


def test_fetch_messages_skips_malformed_lines(chat_on, monkeypatch):
    body = 'not-json\n{"event":"message","time":1,"title":"alice","message":"ok"}\n'
    monkeypatch.setattr(chat.httpx, "get", lambda *a, **k: FakeResponse(text=body))
    assert [m["text"] for m in chat.fetch_messages()] == ["ok"]


def test_fetch_messages_returns_empty_on_error(chat_on, monkeypatch):
    def boom(*a, **k):
        raise httpx.HTTPError("nope")

    monkeypatch.setattr(chat.httpx, "get", boom)
    assert chat.fetch_messages() == []


def test_fetch_messages_disabled(monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_enabled", False)
    assert chat.fetch_messages() == []


def test_publish_posts_topic_and_author(chat_on, monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse({}, 200)

    monkeypatch.setattr(chat.httpx, "post", fake_post)
    chat.publish("alice", "hello")
    assert captured["json"]["topic"] == "labforge"
    assert captured["json"]["title"] == "alice"
    assert captured["json"]["message"] == "hello"


# ---------------------------------------------------------------- routes

def test_chat_messages_partial_renders(web, chat_on, monkeypatch):
    monkeypatch.setattr(
        chat, "fetch_messages",
        lambda: [{"author": "alice", "text": "hi", "time": "12:00"}],
    )
    resp = web.get("/partials/chat/messages")
    assert resp.status_code == 200
    assert "alice" in resp.text
    assert "hi" in resp.text


def test_chat_send_publishes_and_reloads(web, chat_on, monkeypatch):
    calls = []
    monkeypatch.setattr(chat, "publish", lambda author, text: calls.append((author, text)))
    monkeypatch.setattr(chat, "fetch_messages", lambda: [])

    resp = web.post("/chat/send", data={"message": "hello lab"},
                    headers={"X-Forwarded-User": "alice"})
    assert resp.status_code == 200
    assert calls == [("alice", "hello lab")]


def test_chat_send_ignores_blank(web, chat_on, monkeypatch):
    calls = []
    monkeypatch.setattr(chat, "publish", lambda author, text: calls.append((author, text)))
    monkeypatch.setattr(chat, "fetch_messages", lambda: [])

    resp = web.post("/chat/send", data={"message": "   "})
    assert resp.status_code == 200
    assert calls == []
