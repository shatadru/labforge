"""WebSocket proxy tests: the control plane forwards the console to the agent."""
import asyncio

import pytest

from app import ws_proxy


class FakeWS:
    def __init__(self):
        self.closed = None

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def test_agent_ws_url_requires_remote_client():
    with pytest.raises(RuntimeError):
        ws_proxy.agent_ws_url(object(), "/ws/console/x")


def test_agent_ws_url_delegates():
    class Client:
        def subscribe_ws_url(self, path):
            return f"ws://agent{path}"

    assert ws_proxy.agent_ws_url(Client(), "/ws/console/x") == "ws://agent/ws/console/x"


def test_proxy_closes_4501_without_remote_client():
    ws = FakeWS()
    asyncio.run(ws_proxy.proxy_websocket(ws, object(), "/ws/console/x"))
    assert ws.closed is not None
    assert ws.closed[0] == 4501


def test_proxy_closes_4502_when_connect_fails(monkeypatch):
    class Client:
        def subscribe_ws_url(self, path):
            return "ws://agent" + path

    class Connect:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise OSError("refused")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ws_proxy.websockets, "connect", Connect)
    ws = FakeWS()
    asyncio.run(ws_proxy.proxy_websocket(ws, Client(), "/ws/console/x"))
    assert ws.closed[0] == 4502


def test_proxy_forwards_both_directions(monkeypatch):
    class Client:
        def subscribe_ws_url(self, path):
            return "ws://agent" + path

    sent = []

    class Agent:
        def __init__(self):
            self.messages = [b"one", "two"]
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        def __aiter__(self):
            async def gen():
                for m in self.messages:
                    yield m
            return gen()

    agent = Agent()

    class Connect:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return agent

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ws_proxy.websockets, "connect", Connect)

    class WS(FakeWS):
        def __init__(self):
            super().__init__()
            self.received = [
                {"type": "websocket.receive", "text": "hello"},
                {"type": "websocket.disconnect"},
            ]
            self.out = []

        async def receive(self):
            return self.received.pop(0)

        async def send_bytes(self, data):
            self.out.append(("bytes", data))

        async def send_text(self, data):
            self.out.append(("text", data))

    ws = WS()
    asyncio.run(ws_proxy.proxy_websocket(ws, Client(), "/ws/vnc/x"))
    assert ("bytes", b"one") in ws.out
    assert ("text", "two") in ws.out
    assert ws.closed is not None
