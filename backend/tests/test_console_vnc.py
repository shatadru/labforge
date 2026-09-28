"""Console/VNC WebSocket guard tests.

Only the early rejection paths are exercised - never the PTY-spawning success
path, which would exec `virsh console` on the host.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.config import settings
from app import console as console_mod
from app import vnc as vnc_mod
from app.virsh_client import VMInfo


@pytest.fixture
def fake(make_fake_client):
    return make_fake_client(vms=[
        VMInfo(name="labs-run", state="running"),
        VMInfo(name="labs-off", state="shut off"),
    ])


@pytest.fixture
def web(fake, monkeypatch):
    import app.ws_utils as ws_utils
    monkeypatch.setattr(ws_utils, "get_host_client", lambda: fake)
    with TestClient(app) as test_client:
        yield test_client


def _close_code(websocket_cm):
    with pytest.raises(WebSocketDisconnect) as exc:
        with websocket_cm as ws:
            while True:
                ws.receive_text()
    return exc.value.code


# ---------------------------------------------------------------- console

def test_console_disabled(web, monkeypatch):
    monkeypatch.setattr(settings, "console_enabled", False)
    assert _close_code(web.websocket_connect("/ws/console/labs-run")) == 4403


def test_console_unknown_vm(web):
    assert _close_code(web.websocket_connect("/ws/console/labs-ghost")) == 4404


def test_console_vm_not_running(web):
    assert _close_code(web.websocket_connect("/ws/console/labs-off")) == 4409


# ---------------------------------------------------------------- vnc

def test_vnc_disabled(web, monkeypatch):
    monkeypatch.setattr(settings, "console_enabled", False)
    assert _close_code(web.websocket_connect("/ws/vnc/labs-run")) == 4403


def test_vnc_unknown_vm(web):
    assert _close_code(web.websocket_connect("/ws/vnc/labs-ghost")) == 4404


def test_vnc_vm_not_running(web):
    assert _close_code(web.websocket_connect("/ws/vnc/labs-off")) == 4409


def test_vnc_without_display(web, fake):
    fake.display = None
    assert _close_code(web.websocket_connect("/ws/vnc/labs-run")) == 4501


def test_console_session_limit_enforced(web, monkeypatch):
    from app import console as console_mod

    monkeypatch.setattr(settings, "console_max_sessions", 1)
    console_mod._active_sessions["labs-run"] = 1
    try:
        assert _close_code(web.websocket_connect("/ws/console/labs-run")) == 4429
    finally:
        console_mod._active_sessions.clear()


def test_console_rejects_cross_origin(web):
    assert _close_code(
        web.websocket_connect("/ws/console/labs-run", headers={"origin": "http://evil.example"})
    ) == 4403


def test_origin_allowed_unit():
    from app.ws_utils import origin_allowed

    class WS:
        def __init__(self, headers):
            self.headers = headers

    assert origin_allowed(WS({})) is True
    assert origin_allowed(WS({"origin": "http://testserver", "host": "testserver"})) is True
    assert origin_allowed(WS({"origin": "http://evil", "host": "testserver"})) is False
    assert origin_allowed(WS({"origin": "http://evil"})) is False


# ---------------------------------------------------------------- bridges

def test_vnc_tcp_to_ws_forwards_then_stops_on_eof():
    import asyncio
    from app.vnc import _tcp_to_ws

    class Reader:
        def __init__(self):
            self.chunks = [b"one", b"two", b""]

        async def read(self, _n):
            return self.chunks.pop(0)

    class Sock:
        def __init__(self):
            self.sent = []

        async def send_bytes(self, data):
            self.sent.append(data)

    sock = Sock()
    asyncio.run(_tcp_to_ws(Reader(), sock))
    assert sock.sent == [b"one", b"two"]


def test_vnc_ws_to_tcp_writes_bytes_and_ignores_text():
    import asyncio
    from app.vnc import _ws_to_tcp

    class Writer:
        def __init__(self):
            self.data = b""
            self.drained = False

        def write(self, data):
            self.data += data

        async def drain(self):
            self.drained = True

    class Sock:
        def __init__(self):
            self.messages = [
                {"type": "websocket.receive", "bytes": b"hello"},
                {"type": "websocket.receive", "text": "ignored"},
                {"type": "websocket.receive", "bytes": b"world"},
                {"type": "websocket.disconnect"},
            ]

        async def receive(self):
            return self.messages.pop(0)

    writer = Writer()
    asyncio.run(_ws_to_tcp(writer, Sock()))
    assert writer.data == b"helloworld"
    assert writer.drained is True


def test_console_set_winsize_applies_to_pty():
    import os
    import termios
    from app.console import _set_winsize

    master, slave = os.openpty()
    try:
        _set_winsize(master, rows=40, cols=120)
        assert termios.tcgetwinsize(master) == (40, 120)
    finally:
        os.close(master)
        os.close(slave)
