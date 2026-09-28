"""API tests for VM lifecycle + snapshot endpoints (against a fake hypervisor)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api import v1 as api_v1
from app.virsh_client import VMInfo, VirshResult


@pytest.fixture
def client(make_fake_client, monkeypatch):
    fake = make_fake_client(vms=[
        VMInfo(name="labs-run", state="running", ip="192.168.122.10"),
        VMInfo(name="labs-off", state="shut off"),
    ])
    fake.snapshots = {"labs-run": ["clean", "before-upgrade"]}
    monkeypatch.setattr(api_v1, "get_host_client", lambda: fake)
    return fake


@pytest.fixture
def web(client):
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- start / stop

def test_reset_unknown_vm_is_404(web):
    assert web.post("/api/v1/vms/labs-ghost/reset").status_code == 404


def test_snapshot_create_unknown_vm_is_404(web):
    assert web.post("/api/v1/vms/labs-ghost/snapshots",
                    json={"name": "s"}).status_code == 404


def test_list_snapshots_unknown_vm_is_404(web):
    assert web.get("/api/v1/vms/labs-ghost/snapshots").status_code == 404


def test_start_running_vm_is_noop(web, client):
    resp = web.post("/api/v1/vms/labs-run/start")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Already running"
    assert ("start", "labs-run") not in client.calls


def test_start_stopped_vm(web, client):
    resp = web.post("/api/v1/vms/labs-off/start")
    assert resp.status_code == 200
    assert ("start", "labs-off") in client.calls


def test_stop_stopped_vm_is_noop(web, client):
    resp = web.post("/api/v1/vms/labs-off/stop")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Already stopped"


def test_stop_running_vm(web, client):
    resp = web.post("/api/v1/vms/labs-run/stop")
    assert resp.status_code == 200
    assert ("stop", "labs-run") in client.calls


def test_reboot_vm(web, client):
    resp = web.post("/api/v1/vms/labs-run/reboot")
    assert resp.status_code == 200
    assert ("reboot", "labs-run") in client.calls


def test_delete_vm(web, client):
    resp = web.delete("/api/v1/vms/labs-run")
    assert resp.status_code == 200
    assert ("delete", "labs-run") in client.calls


def test_lifecycle_unknown_vm_is_404(web):
    for path in ("/api/v1/vms/labs-ghost/start",
                 "/api/v1/vms/labs-ghost/stop",
                 "/api/v1/vms/labs-ghost/reboot"):
        assert web.post(path).status_code == 404


def test_lifecycle_failure_returns_500(web, client):
    client.fail = True
    resp = web.post("/api/v1/vms/labs-off/start")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "boom"


# ---------------------------------------------------------------- snapshots

def test_list_snapshots(web):
    resp = web.get("/api/v1/vms/labs-run/snapshots")
    assert resp.status_code == 200
    assert resp.json() == ["clean", "before-upgrade"]


def test_create_snapshot(web, client):
    resp = web.post("/api/v1/vms/labs-run/snapshots", json={"name": "new-snap"})
    assert resp.status_code == 200
    assert ("snapshot-create", "labs-run", "new-snap") in client.calls


def test_revert_snapshot(web, client):
    resp = web.post("/api/v1/vms/labs-run/snapshots/clean/revert")
    assert resp.status_code == 200
    assert ("snapshot-revert", "labs-run", "clean") in client.calls


def test_delete_snapshot(web, client):
    resp = web.delete("/api/v1/vms/labs-run/snapshots/clean")
    assert resp.status_code == 200
    assert ("snapshot-delete", "labs-run", "clean") in client.calls


def test_create_snapshot_normalises_name(web, client):
    resp = web.post("/api/v1/vms/labs-run/snapshots", json={"name": "My Snap!"})
    assert resp.status_code == 200
    assert ("snapshot-create", "labs-run", "My-Snap") in client.calls


def test_create_snapshot_rejects_name_with_no_alnum(web):
    resp = web.post("/api/v1/vms/labs-run/snapshots", json={"name": "!!!"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_reset_deletes_vm(web, client):
    resp = web.post("/api/v1/vms/labs-run/reset")
    assert resp.status_code == 200
    assert ("delete", "labs-run") in client.calls


# ---------------------------------------------------------------- single VM

def test_get_single_vm(web):
    resp = web.get("/api/v1/vms/labs-run")
    assert resp.status_code == 200
    assert resp.json()["name"] == "labs-run"


def test_get_single_vm_unknown_is_404(web):
    assert web.get("/api/v1/vms/labs-ghost").status_code == 404


# ---------------------------------------------------------------- failure paths

def test_delete_vm_failure_returns_500(web, client):
    client.fail = True
    assert web.delete("/api/v1/vms/labs-run").status_code == 500


def test_delete_unknown_vm_is_404(web):
    assert web.delete("/api/v1/vms/labs-ghost").status_code == 404


def test_snapshot_revert_rejects_bad_name(web):
    assert web.post("/api/v1/vms/labs-run/snapshots/bad%20name/revert").status_code == 422


def test_create_snapshot_failure_returns_500(web, client):
    client.fail = True
    resp = web.post("/api/v1/vms/labs-run/snapshots", json={"name": "snap"})
    assert resp.status_code == 500


def test_revert_snapshot_failure_returns_500(web, client):
    client.fail = True
    assert web.post("/api/v1/vms/labs-run/snapshots/clean/revert").status_code == 500


def test_delete_snapshot_failure_returns_500(web, client):
    client.fail = True
    assert web.delete("/api/v1/vms/labs-run/snapshots/clean").status_code == 500


def test_reboot_failure_returns_500(web, client):
    client.fail = True
    assert web.post("/api/v1/vms/labs-run/reboot").status_code == 500


def test_stop_failure_returns_500(web, client):
    client.fail = True
    assert web.post("/api/v1/vms/labs-run/stop").status_code == 500
