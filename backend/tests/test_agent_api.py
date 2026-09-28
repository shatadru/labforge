"""Agent API tests: auth, RPC allow-list, serialization, images and WebSockets.

The agent owns libvirt, so these tests pin the security boundary: no token means
no access, only allow-listed methods run, and errors never leak internals.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.agent import (
    _assert_rpc_table_matches_allowlist,
    _bearer_ok,
    _jsonable,
    create_agent_app,
)
from app.config import settings
from app.host_client import RPC_METHODS
from app.virsh_client import TemplateInfo, VirshResult, VMInfo

TOKEN = "unit-test-token-abc123"


@pytest.fixture
def fake(make_fake_client):
    return make_fake_client(
        vms=[VMInfo(name="labs-a", state="running")],
        templates=[TemplateInfo(name="rhel-10", path="/x.qcow2", description="d",
                                memory_mb=2048, vcpus=2, disk_gb=20)],
    )


@pytest.fixture
def agent(monkeypatch, fake):
    monkeypatch.setattr("app.agent.get_virsh_client", lambda: fake)
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    monkeypatch.setattr(settings, "agent_allow_anonymous", False)
    with TestClient(create_agent_app()) as client:
        yield client


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- auth

def test_health_is_unauthenticated(agent):
    resp = agent.get("/agent/v1/health")
    assert resp.status_code == 200
    assert resp.json()["role"] == "agent"


def test_info_requires_token(agent):
    assert agent.get("/agent/v1/info").status_code == 401
    assert agent.get("/agent/v1/info", headers=_auth("wrong")).status_code == 401


def test_info_returns_host_facts(agent):
    resp = agent.get("/agent/v1/info", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["libvirt"] is True
    assert body["templates"] == ["rhel-10"]
    assert "hostname" in body and "version" in body


def test_rpc_rejects_missing_token(agent):
    assert agent.post("/agent/v1/rpc/ping", json={}).status_code == 401


def test_rpc_disallowed_method_is_404(agent):
    resp = agent.post("/agent/v1/rpc/__init__", json={}, headers=_auth())
    assert resp.status_code == 404


def test_rpc_forwards_args(agent, fake):
    resp = agent.post("/agent/v1/rpc/provision_name",
                      json={"args": ["demo", "rhel-10"], "kwargs": {}},
                      headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["result"] == "labs-rhel-10-demo"


def test_rpc_serialises_dataclasses(agent):
    resp = agent.post("/agent/v1/rpc/list_vms", json={"args": [], "kwargs": {}},
                      headers=_auth())
    body = resp.json()["result"]
    assert body[0]["name"] == "labs-a"
    assert isinstance(body[0]["snapshots"], list)


def test_rpc_error_is_generic_500(fake, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("secret internal path /etc/shadow")

    monkeypatch.setattr("app.agent.get_virsh_client", lambda: fake)
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    fake.ping = boom  # the table captures bound methods at app creation
    with TestClient(create_agent_app()) as client:
        resp = client.post("/agent/v1/rpc/ping", json={}, headers=_auth())
    assert resp.status_code == 500
    assert "secret internal path" not in resp.text
    assert "Traceback" not in resp.text


def test_rpc_table_matches_allowlist(fake):
    assert set(_assert_rpc_table_matches_allowlist(fake)) == set(RPC_METHODS)


def test_bearer_ok_anonymous_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "agent_token", None)
    monkeypatch.setattr(settings, "agent_allow_anonymous", False)
    assert _bearer_ok(None) is False
    monkeypatch.setattr(settings, "agent_allow_anonymous", True)
    assert _bearer_ok(None) is True


def test_jsonable_handles_types():
    from app.virsh_client import VMInfo

    assert _jsonable(Path("/tmp/x")) == "/tmp/x"
    assert _jsonable({"a": [1, 2]}) == {"a": [1, 2]}
    assert _jsonable(VMInfo(name="labs-a", state="running"))["name"] == "labs-a"
    assert _jsonable(VMInfo) is VMInfo  # class objects are not turned into dicts


# ---------------------------------------------------------------- websockets

def test_ws_console_rejects_missing_token(agent):
    with pytest.raises(WebSocketDisconnect) as exc:
        with agent.websocket_connect("/agent/v1/ws/console/labs-a"):
            pass
    assert exc.value.code == 4401


def test_ws_console_accepts_with_token(agent, monkeypatch):
    called = {}

    async def fake_stream(ws, vm_name, client):
        called["vm"] = vm_name

    monkeypatch.setattr("app.agent.stream_console", fake_stream)
    with agent.websocket_connect("/agent/v1/ws/console/labs-a",
                                 headers=_auth()) as ws:
        ws.close()
    assert called["vm"] == "labs-a"


def test_ws_vnc_rejects_missing_token(agent):
    with pytest.raises(WebSocketDisconnect) as exc:
        with agent.websocket_connect("/agent/v1/ws/vnc/labs-a"):
            pass
    assert exc.value.code == 4401


# ---------------------------------------------------------------- images

def test_image_upload_requires_token(agent):
    resp = agent.post("/agent/v1/images",
                      files={"file": ("x.qcow2", b"data", "application/octet-stream")})
    assert resp.status_code == 401


def test_image_upload_stores_qcow2(agent, fake, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    fake.accept_uploads = True
    resp = agent.post(
        "/agent/v1/images",
        files={"file": ("tiny.qcow2", b"x" * 16, "application/octet-stream")},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert (tmp_path / "tiny.qcow2").exists()
    assert not list(tmp_path.glob(".tiny*"))
