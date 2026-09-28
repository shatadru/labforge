"""Remote host client tests: the control-plane half of the agent contract."""
import httpx
import pytest

from app import host_client
from app.config import settings
from app.host_client import HostError, RemoteHostClient, get_host_client, host_mode
from app.virsh_client import VirshResult, VMInfo


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200, {"result": None})

    monkeypatch.setattr(host_client.httpx, "post", fake_post)
    return calls


def test_host_mode_forces_local_in_agent_mode(monkeypatch):
    monkeypatch.setattr(settings, "mode", "agent")
    monkeypatch.setattr(settings, "host_mode", "remote")
    assert host_mode() == "local"


def test_get_host_client_remote_requires_url(monkeypatch):
    monkeypatch.setattr(settings, "mode", "control")
    monkeypatch.setattr(settings, "host_mode", "remote")
    monkeypatch.setattr(settings, "agent_url", None)
    with pytest.raises(HostError):
        get_host_client()


def test_get_host_client_remote_strips_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "mode", "control")
    monkeypatch.setattr(settings, "host_mode", "remote")
    monkeypatch.setattr(settings, "agent_url", "http://h:8443/")
    client = get_host_client()
    assert isinstance(client, RemoteHostClient)
    assert client.base_url == "http://h:8443"


def test_call_sends_bearer_and_args(captured):
    client = RemoteHostClient("http://h:8443", token="s3cr3t")
    client.provision_name("demo", "rhel-10")
    url, kwargs = captured[0]
    assert url == "http://h:8443/agent/v1/rpc/provision_name"
    assert kwargs["headers"]["Authorization"] == "Bearer s3cr3t"
    assert kwargs["json"] == {"args": ["demo", "rhel-10"], "kwargs": {}}


def test_call_omits_header_without_token(captured):
    client = RemoteHostClient("http://h:8443")
    client.ping()
    assert "Authorization" not in captured[0][1]["headers"]


def test_401_becomes_host_error(monkeypatch):
    monkeypatch.setattr(host_client.httpx, "post",
                        lambda *a, **k: FakeResponse(401))
    with pytest.raises(HostError, match="rejected the token"):
        RemoteHostClient("http://h:8443", token="x").ping()


def test_5xx_becomes_host_error(monkeypatch):
    monkeypatch.setattr(host_client.httpx, "post",
                        lambda *a, **k: FakeResponse(500, text="boom"))
    with pytest.raises(HostError, match="agent error 500"):
        RemoteHostClient("http://h:8443").ping()


def test_network_error_is_unreachable(monkeypatch):
    def raise_connect(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(host_client.httpx, "post", raise_connect)
    with pytest.raises(HostError, match="unreachable"):
        RemoteHostClient("http://h:8443").ping()


def test_decodes_get_vm_and_lists(monkeypatch):
    vm = {"name": "labs-a", "state": "running", "snapshots": []}
    responses = {
        "get_vm": {"result": vm},
        "list_vms": {"result": [vm]},
    }

    def fake_post(url, **kwargs):
        return FakeResponse(200, responses[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(host_client.httpx, "post", fake_post)
    client = RemoteHostClient("http://h:8443")
    assert isinstance(client.get_vm("labs-a"), VMInfo)
    assert isinstance(client.list_vms()[0], VMInfo)


def test_decodes_virsh_result(monkeypatch):
    monkeypatch.setattr(
        host_client.httpx, "post",
        lambda *a, **k: FakeResponse(200, {"result": {"success": True, "stdout": "ok"}}),
    )
    result = RemoteHostClient("http://h:8443").start_vm("labs-a")
    assert isinstance(result, VirshResult) and result.success is True


def test_upload_image_posts_multipart(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200, {"name": "tiny.qcow2"})

    monkeypatch.setattr(host_client.httpx, "post", fake_post)
    client = RemoteHostClient("http://h:8443", token="t")
    out = client.upload_image("tiny.qcow2", object(), "application/octet-stream")
    assert out["name"] == "tiny.qcow2"
    assert calls[0][0] == "http://h:8443/agent/v1/images"
    assert calls[0][1]["files"]["file"][0] == "tiny.qcow2"


def test_unknown_method_raises_attribute_error():
    client = RemoteHostClient("http://h:8443")
    with pytest.raises(AttributeError):
        client.not_a_method


def test_subscribe_ws_url_schemes():
    assert (RemoteHostClient("http://h:8443").subscribe_ws_url("/ws/console/x")
            == "ws://h:8443/agent/v1/ws/console/x")
    assert (RemoteHostClient("https://h:8443").subscribe_ws_url("/ws/vnc/x")
            == "wss://h:8443/agent/v1/ws/vnc/x")
