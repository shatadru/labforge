"""API tests - host metrics and provisioning validation."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.api import v1 as api_v1
from app.virsh_client import VMInfo, VirshResult


@pytest.fixture
def web(make_fake_client, monkeypatch):
    """Empty fake hypervisor so provisioning tests start from a clean slate."""
    client = make_fake_client()
    monkeypatch.setattr(api_v1, "get_host_client", lambda: client)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- host metrics

def test_host_usage_endpoint(web, monkeypatch, sample_host_usage):
    monkeypatch.setattr(api_v1, "get_host_usage", lambda: sample_host_usage)
    resp = web.get("/api/v1/host/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cpu_percent"] == 14.2
    assert data["cpu_cores"] == 16
    assert data["mem_percent"] == 68.2
    assert data["disk_available_gb"] == 99


def test_host_check_endpoint(web, monkeypatch, sample_host_usage):
    monkeypatch.setattr(api_v1, "get_host_usage", lambda: sample_host_usage)
    resp = web.get("/api/v1/host/check", params={"memory_mb": 2048, "vcpus": 2, "disk_gb": 20})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_host_check_reports_shortfall(web, monkeypatch, sample_host_usage):
    monkeypatch.setattr(api_v1, "get_host_usage", lambda: sample_host_usage)
    # 64 GB memory needed but only ~9.2 GB free
    resp = web.get("/api/v1/host/check", params={"memory_mb": 65536, "vcpus": 2, "disk_gb": 20})
    body = resp.json()
    assert body["ok"] is False
    assert any("memory" in r.lower() for r in body["reasons"])


def test_host_check_includes_fit_count(web, monkeypatch, sample_host_usage):
    monkeypatch.setattr(api_v1, "get_host_usage", lambda: sample_host_usage)
    body = web.get("/api/v1/host/check", params={"memory_mb": 2048, "vcpus": 2, "disk_gb": 20}).json()
    assert body["ok"] is True
    assert body["fits"] >= 1
    assert body["capacity"]["budget_percent"] == 50


def test_host_capacity_endpoint(web, monkeypatch, sample_host_usage, sample_templates):
    client = api_v1.get_host_client()
    client.templates = sample_templates
    monkeypatch.setattr(api_v1, "get_host_usage", lambda: sample_host_usage)

    body = web.get("/api/v1/host/capacity").json()
    assert body["budget_percent"] == 50
    assert body["budget"]["vcpus"] == 8          # 50% of 16 cores
    assert body["used"]["vms"] == 0
    names = {t["name"]: t["fits"] for t in body["templates"]}
    assert names["rhel-10"] >= 1


# ---------------------------------------------------------------- guest usage

def _guest_usage(name="labs-a", available=True):
    from app.vm_metrics import GuestUsage
    return GuestUsage(
        name=name, available=available, cpu_percent=12.5, vcpus=2,
        mem_total_mb=2000, mem_used_mb=600, mem_percent=30.0,
        disk_total_gb=30.0, disk_used_gb=1.5, disk_percent=5.0,
        load_1m=0.1, load_5m=0.2, load_15m=0.3, sampled_at=123.0,
    )


def test_vm_usage_endpoint(web, monkeypatch, make_fake_client):
    client = make_fake_client(vms=[VMInfo(name="labs-a", state="running", vcpus=2)])
    monkeypatch.setattr(api_v1, "get_host_client", lambda: client)
    monkeypatch.setattr(api_v1, "usage_for", lambda c, n: _guest_usage(n))

    resp = web.get("/api/v1/vms/labs-a/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["cpu_percent"] == 12.5
    assert data["disk_percent"] == 5.0
    assert data["mem_used_mb"] == 600


def test_vm_usage_unknown_is_404(web, monkeypatch, make_fake_client):
    client = make_fake_client()
    monkeypatch.setattr(api_v1, "get_host_client", lambda: client)
    assert web.get("/api/v1/vms/nope/usage").status_code == 404


def test_vm_usage_not_collected_yet(web, monkeypatch, make_fake_client):
    client = make_fake_client(vms=[VMInfo(name="labs-a", state="running", vcpus=2)])
    monkeypatch.setattr(api_v1, "get_host_client", lambda: client)
    monkeypatch.setattr(api_v1, "usage_for", lambda c, n: None)
    data = web.get("/api/v1/vms/labs-a/usage").json()
    assert data["available"] is False
    assert data["reason"]


def test_health_endpoint(web):
    resp = web.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_api_key_enforced_when_configured(web, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-key")
    assert web.get("/api/v1/vms").status_code == 401
    assert web.get("/api/v1/vms", headers={"X-API-Key": "wrong"}).status_code == 401
    assert web.get("/api/v1/vms",
                   headers={"X-API-Key": "s3cret-key"}).status_code == 200
    assert web.get("/api/v1/vms",
                   headers={"Authorization": "Bearer s3cret-key"}).status_code == 200


def test_api_key_disabled_by_default(web):
    # No API_KEY configured means the trusted origin stays open (UI keeps working).
    assert settings.api_key is None
    assert web.get("/api/v1/vms").status_code == 200


def test_readiness_endpoint(web, monkeypatch, tmp_path):
    class Ready:
        def ping(self):
            return True

    monkeypatch.setattr("app.main.get_host_client", lambda: Ready())
    monkeypatch.setattr(settings, "templates_dir", str(tmp_path))
    body = web.get("/api/ready").json()
    assert body["ready"] is True
    assert body["checks"] == {"host": True, "templates": True}


def test_readiness_reports_not_ready(web, monkeypatch, tmp_path):
    class Broken:
        def ping(self):
            return False

    monkeypatch.setattr("app.main.get_host_client", lambda: Broken())
    monkeypatch.setattr(settings, "templates_dir", str(tmp_path))
    resp = web.get("/api/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["host"] is False


def test_host_check_rejects_invalid_params(web):
    assert web.get("/api/v1/host/check", params={"vcpus": 0}).status_code == 422
    assert web.get("/api/v1/host/check", params={"memory_mb": 1}).status_code == 422


# ---------------------------------------------------------------- provisioning

def _provision(web, **overrides):
    payload = {"name": "demo", "template": "rhel-10", "memory_mb": 2048, "vcpus": 2, "disk_gb": 20}
    payload.update(overrides)
    return web.post("/api/v1/vms", json=payload)


def test_provision_without_tailscale(web, monkeypatch):
    client = api_v1.get_host_client()
    resp = _provision(web, enable_tailscale=False)
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert client.create_calls[-1]["enable_tailscale"] is False


def test_provision_tailscale_disabled(web):
    client = api_v1.get_host_client()
    resp = _provision(web, enable_tailscale=False)
    assert resp.status_code == 200
    call = client.create_calls[-1]
    assert call["enable_tailscale"] is False


def test_provision_tailscale_enabled_by_default(web):
    client = api_v1.get_host_client()
    resp = _provision(web)  # enable_tailscale defaults to True
    assert resp.status_code == 200
    assert client.create_calls[-1]["enable_tailscale"] is True


def test_provision_ignores_unknown_tailscale_key_field(web):
    """The API no longer accepts an inline key; provisioning still succeeds."""
    client = api_v1.get_host_client()
    resp = _provision(web, enable_tailscale=True, tailscale_auth_key="tskey-runtime")
    assert resp.status_code == 200
    call = client.create_calls[-1]
    assert call["enable_tailscale"] is True
    assert "tailscale_auth_key" not in call


def test_provision_accepts_explicit_tailscale_disable(web):
    client = api_v1.get_host_client()
    resp = _provision(web, enable_tailscale=False)
    assert resp.status_code == 200
    assert client.create_calls[-1]["enable_tailscale"] is False


def test_provision_accepts_injected_credentials(web):
    client = api_v1.get_host_client()
    resp = _provision(web, vm_user="deploy", vm_password="hunter2")
    assert resp.status_code == 200
    call = client.create_calls[-1]
    assert call["vm_user"] == "deploy"
    assert call["vm_password"] == "hunter2"


def test_provision_credentials_default_to_none(web):
    client = api_v1.get_host_client()
    _provision(web)
    call = client.create_calls[-1]
    assert call["vm_user"] is None
    assert call["vm_password"] is None


def test_provision_rejects_invalid_vm_user(web):
    assert _provision(web, vm_user="Bad User!").status_code == 422


def test_provision_rejects_duplicate_name(web):
    client = api_v1.get_host_client()
    client.vms = [VMInfo(name="labs-rhel-10-demo", state="running")]
    resp = _provision(web)
    assert resp.status_code == 409


def test_provision_empty_after_normalising_is_422(web):
    resp = _provision(web, name="///")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_provision_reports_create_failure(web, monkeypatch):
    client = api_v1.get_host_client()

    def boom(**kwargs):
        return VirshResult(success=False, stderr="Host resources insufficient: nope")

    monkeypatch.setattr(client, "create_vm", boom)
    resp = _provision(web)
    assert resp.status_code == 500
    assert "insufficient" in resp.json()["detail"].lower()


def test_provision_normalizes_name_with_prefix(web):
    client = api_v1.get_host_client()
    resp = _provision(web, name="myvm")
    assert resp.status_code == 200
    assert client.create_calls[-1]["name"] == "labs-rhel-10-myvm"


def test_provision_accepts_uppercase_and_spaces(web):
    client = api_v1.get_host_client()
    resp = _provision(web, name="My VM")
    assert resp.status_code == 200
    assert client.create_calls[-1]["name"] == "labs-rhel-10-my-vm"
    assert resp.json()["name"] == "labs-rhel-10-my-vm"


def test_provision_rejects_a_name_with_no_letters_or_digits(web):
    resp = _provision(web, name="!!!")
    assert resp.status_code == 422
    # The detail must be a readable string, never an object.
    assert isinstance(resp.json()["detail"], str)


def test_provision_lowercases_login_user(web):
    client = api_v1.get_host_client()
    resp = _provision(web, vm_user="Deploy")
    assert resp.status_code == 200
    assert client.create_calls[-1]["vm_user"] == "deploy"


# ---------------------------------------------------------------- listing

def test_list_vms_endpoint(web):
    client = api_v1.get_host_client()
    client.vms = [VMInfo(name="labs-demo", state="running", ip="192.168.122.10",
                         tailscale_ip="100.101.102.103")]
    resp = web.get("/api/v1/vms")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["name"] == "labs-demo"


# ---------------------------------------------------------------- uploaded images

def test_list_images_endpoint(web):
    client = api_v1.get_host_client()
    client.images = [{"name": "x.qcow2", "size_gb": 1.0, "disk_gb": 10}]
    resp = web.get("/api/v1/images")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "x.qcow2"


def test_upload_image(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    client = api_v1.get_host_client()
    client.accept_uploads = True
    resp = web.post("/api/v1/images",
                    files={"file": ("tiny.qcow2", b"not-really-qcow2", "application/octet-stream")})
    assert resp.status_code == 200
    assert resp.json()["name"] == "tiny.qcow2"
    assert (tmp_path / "tiny.qcow2").is_file()
    assert not list(tmp_path.glob("*.part"))


def test_upload_image_rejects_non_qcow2(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    client = api_v1.get_host_client()
    client.accept_uploads = False
    resp = web.post("/api/v1/images",
                    files={"file": ("bad.qcow2", b"x", "application/octet-stream")})
    assert resp.status_code == 422
    assert not (tmp_path / "bad.qcow2").exists()
    assert not list(tmp_path.glob("*.part"))


def test_upload_image_rejects_duplicate(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    (tmp_path / "dup.qcow2").write_bytes(b"x")
    resp = web.post("/api/v1/images",
                    files={"file": ("dup.qcow2", b"x", "application/octet-stream")})
    assert resp.status_code == 409


def test_provision_requires_exactly_one_source(web):
    assert web.post("/api/v1/vms", json={"name": "demo"}).status_code == 422
    assert web.post("/api/v1/vms",
                    json={"name": "demo", "template": "rhel-10",
                          "image": "x.qcow2"}).status_code == 422


def test_provision_from_uploaded_image(web):
    client = api_v1.get_host_client()
    resp = web.post("/api/v1/vms", json={"name": "demo", "image": "x.qcow2",
                                         "memory_mb": 1536, "vcpus": 2, "disk_gb": 30})
    assert resp.status_code == 200
    call = client.create_calls[-1]
    assert call["image"] == "x.qcow2"
    assert call["template_name"] is None


def test_delete_image(web, tmp_path):
    client = api_v1.get_host_client()
    target = tmp_path / "gone.qcow2"
    target.write_bytes(b"x")
    client.resolve_image = lambda ref: target
    resp = web.delete("/api/v1/images/gone.qcow2")
    assert resp.status_code == 200
    assert not target.exists()


def test_upload_image_returns_disk_size(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    client = api_v1.get_host_client()
    client.accept_uploads = True
    resp = web.post("/api/v1/images",
                    files={"file": ("tiny.qcow2", b"x", "application/octet-stream")})
    assert resp.status_code == 200
    assert resp.json()["disk_gb"] == 10
    assert resp.json()["format"] == "qcow2"


def test_upload_image_adds_qcow2_extension(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    api_v1.get_host_client().accept_uploads = True
    resp = web.post("/api/v1/images",
                    files={"file": ("plain", b"x", "application/octet-stream")})
    assert resp.status_code == 200
    assert resp.json()["name"] == "plain.qcow2"


def test_upload_image_rejects_non_image_extension(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    resp = web.post("/api/v1/images",
                    files={"file": ("notes.txt", b"x", "text/plain")})
    assert resp.status_code == 422
    assert not (tmp_path / "notes.txt").exists()


def test_upload_image_rejects_zero_byte(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    api_v1.get_host_client().accept_uploads = False
    resp = web.post("/api/v1/images",
                    files={"file": ("empty.qcow2", b"", "application/octet-stream")})
    assert resp.status_code == 422
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / "empty.qcow2").exists()


def test_upload_image_rejects_oversize(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    monkeypatch.setattr(settings, "max_upload_gb", 0)  # any byte exceeds the cap
    api_v1.get_host_client().accept_uploads = True
    resp = web.post("/api/v1/images",
                    files={"file": ("big.qcow2", b"x" * 32, "application/octet-stream")})
    assert resp.status_code == 413
    assert not (tmp_path / "big.qcow2").exists()
    assert not list(tmp_path.glob("*.part"))


def test_delete_image_unknown_is_404(web):
    # The default fake has no import_base, so resolve_image raises ValueError.
    assert web.delete("/api/v1/images/nope.qcow2").status_code == 404


def test_concurrent_uploads_same_name_only_one_wins(web, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    api_v1.get_host_client().accept_uploads = True
    import concurrent.futures as cf

    def post():
        return web.post("/api/v1/images",
                        files={"file": ("race.qcow2", b"x" * 64, "application/octet-stream")}).status_code

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(lambda _: post(), range(4)))
    assert codes.count(200) == 1
    assert codes.count(409) == 3
    assert (tmp_path / "race.qcow2").is_file()
    assert not list(tmp_path.glob("*.part"))


# ---------------------------------------------------------------- templates + remote

def test_list_templates_endpoint(web, monkeypatch, sample_templates):
    client = api_v1.get_host_client()
    client.templates = sample_templates
    body = web.get("/api/v1/templates").json()
    names = {t["name"] for t in body}
    assert "rhel-10" in names


def test_upload_image_remote_forwards_to_agent(web, monkeypatch, tmp_path):
    calls = {}

    def fake_upload(filename, stream, content_type=None):
        calls["filename"] = filename
        return {"name": filename, "disk_gb": 10}

    client = api_v1.get_host_client()
    monkeypatch.setattr(client, "upload_image", fake_upload)
    monkeypatch.setattr(api_v1, "host_mode", lambda: "remote")
    resp = web.post("/api/v1/images",
                    files={"file": ("tiny.qcow2", b"x", "application/octet-stream")})
    assert resp.status_code == 200
    assert calls["filename"] == "tiny.qcow2"
    assert not list(tmp_path.glob("*.qcow2"))


def test_upload_image_remote_maps_host_error_to_502(web, monkeypatch):
    from app.host_client import HostError

    def boom(*_a, **_k):
        raise HostError("agent down")

    client = api_v1.get_host_client()
    monkeypatch.setattr(client, "upload_image", boom)
    monkeypatch.setattr(api_v1, "host_mode", lambda: "remote")
    resp = web.post("/api/v1/images",
                    files={"file": ("tiny.qcow2", b"x", "application/octet-stream")})
    assert resp.status_code == 502
    assert "agent down" in resp.json()["detail"]
