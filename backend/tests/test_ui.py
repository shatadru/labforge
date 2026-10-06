"""UI tests - every page and HTMX partial must render with real data.

These are the tests that would have caught the ``'cpu_percent' is undefined``
regression: each template is rendered through the real FastAPI app with strict
Jinja2 undefined handling, so a missing variable fails loudly.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import ui
from app.virsh_client import VMInfo


@pytest.fixture
def web(fake_client):
    with TestClient(app) as client:
        yield client


def _no_undefined(body: str) -> bool:
    return "is undefined" not in body and "UndefinedError" not in body


# ---------------------------------------------------------------- dashboard

def test_dashboard_renders_with_vms(web, monkeypatch, fake_client, sample_host_usage):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    resp = web.get("/")
    assert resp.status_code == 200
    body = resp.text

    # host card is loaded via HTMX (never server-included without data)
    assert 'hx-get="/partials/host"' in body
    assert "Host Resources" in body
    assert 'hx-trigger="load, every 3s"' in body

    # lab capacity card is loaded via HTMX too
    assert 'hx-get="/partials/capacity"' in body
    assert "Lab capacity" in body

    # VMs render, and the Tailscale IP is preferred for SSH
    assert "labs-demo" in body
    assert "100.101.102.103" in body
    assert "cloud-user@100.101.102.103" in body
    assert "labs-vm2" in body

    # activity sparkline script is wired up
    assert "/static/activity.js" in body

    assert _no_undefined(body), body


def test_dashboard_empty_state(web, monkeypatch, make_fake_client):
    empty = make_fake_client(templates=[])
    monkeypatch.setattr(ui, "get_host_client", lambda: empty)

    resp = web.get("/")
    assert resp.status_code == 200
    assert "No lab VMs yet." in resp.text
    assert "Provision your first VM" in resp.text
    assert _no_undefined(resp.text)


def test_dashboard_falls_back_to_libvirt_ip(web, monkeypatch, make_fake_client):
    only_ip = make_fake_client(vms=[VMInfo(name="labs-plain", state="running", ip="192.168.122.55")])
    monkeypatch.setattr(ui, "get_host_client", lambda: only_ip)

    body = web.get("/").text
    assert "192.168.122.55" in body
    assert "cloud-user@192.168.122.55" in body
    assert _no_undefined(body)


def test_vm_card_uses_per_vm_login_user(web, monkeypatch, make_fake_client):
    custom = make_fake_client(vms=[
        VMInfo(name="labs-a", state="running", ip="192.168.122.7", user="deploy"),
    ])
    monkeypatch.setattr(ui, "get_host_client", lambda: custom)

    body = web.get("/").text
    assert "deploy@192.168.122.7" in body
    assert "cloud-user@192.168.122.7" not in body


def test_vm_card_notes_tailscale_joining_with_fallback(web, monkeypatch, make_fake_client):
    """A running VM with a libvirt IP but pending Tailscale shows it is joining."""
    pending = make_fake_client(vms=[
        VMInfo(name="labs-ts", state="running", ip="192.168.122.9", tailscale_enabled=True),
    ])
    monkeypatch.setattr(ui, "get_host_client", lambda: pending)

    body = web.get("/").text
    assert "Waiting for the Tailscale IP" in body
    # The libvirt address is still offered as a fallback.
    assert "192.168.122.9" in body


def test_vm_card_shows_waiting_for_tailscale_ip(web, monkeypatch, make_fake_client):
    """No address at all yet, but Tailscale was requested, so say what we wait for."""
    pending = make_fake_client(vms=[
        VMInfo(name="labs-ts", state="running", tailscale_enabled=True),
    ])
    monkeypatch.setattr(ui, "get_host_client", lambda: pending)

    body = web.get("/").text
    assert "Waiting for Tailscale IP" in body


def test_vm_card_shows_waiting_for_network_address(web, monkeypatch, make_fake_client):
    plain = make_fake_client(vms=[VMInfo(name="labs-plain", state="running")])
    monkeypatch.setattr(ui, "get_host_client", lambda: plain)

    body = web.get("/").text
    assert "Waiting for a network address" in body


def test_vm_status_partial_renders(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    resp = web.get("/partials/vm-status/labs-demo")
    assert resp.status_code == 200
    assert "status-badge" in resp.text
    assert "running" in resp.text


def test_templates_page_warns_when_tailscale_unavailable(web, monkeypatch, fake_client,
                                                         sample_host_usage):
    fake_client.tailscale_ok = False
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    body = web.get("/templates").text
    assert "No tailnet auth key is configured" in body
    assert "disabled" in body


def test_templates_page_enables_tailscale_when_available(web, monkeypatch, fake_client,
                                                         sample_host_usage):
    fake_client.tailscale_ok = True
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    body = web.get("/templates").text
    assert 'name="enable_tailscale" checked' in body
    assert "No tailnet auth key is configured" not in body


# ---------------------------------------------------------------- templates page

def test_templates_page_renders(web, monkeypatch, fake_client, sample_host_usage):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    resp = web.get("/templates")
    assert resp.status_code == 200
    body = resp.text
    assert "rhel-10" in body and "fedora-44" in body
    assert "Provision VM" in body
    # runtime Tailscale controls exist on the form
    assert 'name="enable_tailscale"' in body
    # no inline key field - uses server-side key file
    assert 'name="tailscale_auth_key"' not in body
    # per-VM credentials are injectable
    assert 'name="vm_user"' in body
    assert 'name="vm_password"' in body
    # capacity feedback
    assert "allocated" in body
    assert "fits 2" in body
    # accessible provision dialog
    assert 'role="dialog"' in body
    assert 'aria-modal="true"' in body
    assert 'role="status"' in body
    assert _no_undefined(body)


def test_templates_page_shows_no_capacity_when_full(web, monkeypatch, fake_client):
    from app.host import HostUsage

    tiny = HostUsage(
        cpu_percent=1.0, mem_total_mb=4000, mem_available_mb=3000, mem_used_mb=1000,
        mem_percent=25.0, load_1m=0.0, load_5m=0.0, load_15m=0.0, cpu_cores=2,
        disk_total_gb=40, disk_available_gb=30, disk_used_gb=10, disk_percent=25.0,
    )
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: tiny)

    body = web.get("/templates").text
    assert "no capacity" in body
    assert "disabled" in body
    assert _no_undefined(body)


def test_templates_page_empty(web, monkeypatch, make_fake_client):
    empty = make_fake_client(templates=[])
    monkeypatch.setattr(ui, "get_host_client", lambda: empty)

    resp = web.get("/templates")
    assert resp.status_code == 200
    assert _no_undefined(resp.text)


# ---------------------------------------------------------------- vm detail

def test_vm_detail_renders(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)

    resp = web.get("/vms/labs-demo")
    assert resp.status_code == 200
    body = resp.text
    assert "labs-demo" in body
    assert "100.101.102.103" in body  # Tailscale IP preferred
    assert "clean" in body            # snapshot listed
    assert _no_undefined(body)


def test_vm_detail_unknown_returns_404(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    resp = web.get("/vms/definitely-not-a-vm")
    assert resp.status_code == 404


# ---------------------------------------------------------------- partials

def test_host_partial_renders_live_metrics(web, monkeypatch, sample_host_usage):
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    resp = web.get("/partials/host")
    assert resp.status_code == 200
    body = resp.text

    assert "14.2%" in body           # CPU
    assert "16 cores" in body
    assert "68.2%" in body           # memory
    assert "9469 MB free / 29760 MB" in body
    assert "78.2%" in body           # disk
    assert "99 GB free / 465 GB" in body
    assert "0.97 / 0.92 / 0.85" in body

    # activity-monitor hooks
    assert body.count('data-spark=') == 3
    assert 'data-cpu="14.2"' in body

    assert _no_undefined(body)


def test_capacity_partial_renders(web, monkeypatch, fake_client, sample_host_usage):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    resp = web.get("/partials/capacity")
    assert resp.status_code == 200
    body = resp.text
    assert "Lab capacity" in body
    assert "budget 50% of host" in body
    assert "2 VM" in body            # sample_vms
    assert "4 / 8 vCPU" in body      # used / budget
    assert "4096 / 14880 MB" in body
    assert "≈ 2 more fit" in body
    assert _no_undefined(body)


def test_vms_partial_renders(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    resp = web.get("/partials/vms")
    assert resp.status_code == 200
    assert "labs-demo" in resp.text
    assert _no_undefined(resp.text)


def test_vm_card_partial_renders(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    resp = web.get("/partials/vm-card/labs-demo")
    assert resp.status_code == 200
    assert "labs-demo" in resp.text
    assert _no_undefined(resp.text)


def test_vm_card_partial_unknown_is_empty(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    resp = web.get("/partials/vm-card/nope")
    assert resp.status_code == 200
    assert resp.text == ""


# ---------------------------------------------------------------- guest usage

def _usage(name="labs-demo"):
    from app.vm_metrics import GuestUsage
    return GuestUsage(
        name=name, available=True, cpu_percent=12.5, vcpus=2,
        mem_total_mb=2000, mem_used_mb=600, mem_percent=30.0,
        disk_total_gb=30.0, disk_used_gb=1.5, disk_percent=5.0,
        load_1m=0.1, load_5m=0.2, load_15m=0.3, sampled_at=1.0,
    )


def test_dashboard_vm_card_shows_guest_usage(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "usage_map", lambda c: {"labs-demo": _usage()})
    body = web.get("/").text
    assert "vm-usage-strip" in body
    assert "12.5%" in body
    assert _no_undefined(body)


def test_vm_detail_shows_guest_resources(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "usage_for", lambda c, n: _usage(n))
    body = web.get("/vms/labs-demo").text
    assert "VM Resources" in body
    assert "GUEST · inside the VM" in body
    assert "12.5%" in body
    assert 'hx-get="/partials/vm-usage/labs-demo"' in body
    assert _no_undefined(body)


def test_vm_usage_partial_unavailable(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "usage_for", lambda c, n: None)
    body = web.get("/partials/vm-usage/labs-demo").text
    assert "Collecting guest metrics" in body
    assert _no_undefined(body)


def test_vm_usage_partial_renders_agent_scope(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "usage_for", lambda c, n: _usage(n))
    body = web.get("/partials/vm-usage/labs-demo").text
    assert 'data-usage="vm-labs-demo"' in body
    assert body.count('data-spark=') == 3
    assert "not host usage" in body


# ---------------------------------------------------------------- static assets

@pytest.mark.parametrize("asset", ["/static/app.css", "/static/activity.js", "/static/console.js",
                                   "/static/htmx.min.js"])
def test_static_assets_served(web, asset):
    resp = web.get(asset)
    assert resp.status_code == 200
    assert resp.content


def test_htmx_is_vendored_not_cdn(web):
    """The UI must work without an external CDN."""
    body = web.get("/").text
    assert "/static/htmx.min.js" in body
    assert "unpkg.com" not in body
    assert "cdn.jsdelivr.net" not in body


def test_css_defines_classes_used_by_templates(web):
    """Guard against dropping styles for classes the templates still use."""
    css = web.get("/static/app.css").text
    for cls in [".sidebar", ".host-stats", ".vm-card", ".btn-primary", ".modal",
                ".template-card", ".detail-grid", ".status-running", ".spark"]:
        assert cls in css, f"missing CSS for {cls}"


def test_templates_page_has_image_source_and_upload_controls(web, monkeypatch, fake_client,
                                                             sample_host_usage):
    fake_client.tailscale_ok = True
    fake_client.images = [{"name": "uploaded.qcow2", "size_gb": 1.0, "disk_gb": 20}]
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    body = web.get("/templates").text
    for needle in [
        "showImageForm()",
        'id="form-source"',
        'id="form-image"',
        'id="upload-file"',
        "uploadImage()",
        'role="progressbar"',
        "resetUpload",
        "uploaded.qcow2",
    ]:
        assert needle in body, needle
    assert _no_undefined(body)


def test_templates_page_explains_no_capacity(web, monkeypatch, fake_client):
    from app.host import HostUsage

    tiny = HostUsage(
        cpu_percent=1.0, mem_total_mb=4000, mem_available_mb=3000, mem_used_mb=1000,
        mem_percent=25.0, load_1m=0.0, load_5m=0.0, load_15m=0.0, cpu_cores=2,
        disk_total_gb=40, disk_available_gb=30, disk_used_gb=10, disk_percent=25.0,
    )
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: tiny)

    body = web.get("/templates").text
    assert "Not enough budget" in body
    assert "GB free." in body


def test_layout_has_live_server_status_indicator(web):
    body = web.get("/").text
    assert 'id="server-status"' in body
    assert 'id="server-dot"' in body
    assert 'id="server-status-text"' in body
    assert 'role="status"' in body
    # The poller lives in app.js and is loaded on every page.
    assert "/static/app.js" in body


def test_templates_page_has_drop_zone_and_browse(web, monkeypatch, fake_client, sample_host_usage):
    fake_client.tailscale_ok = True
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    monkeypatch.setattr(ui, "get_host_usage", lambda: sample_host_usage)

    body = web.get("/templates").text
    assert 'id="upload-drop"' in body
    assert "initUploadDropZone" in body
    # The file input must be a normal visible control, not hidden.
    assert 'id="upload-file"' in body
    assert 'id="upload-file" hidden' not in body
    # And the no-dialog fallback path is shown.
    assert "on the lab host and pick it from the list" in body


def test_vm_detail_snapshot_form_posts_json(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    body = web.get("/vms/labs-demo").text
    assert 'data-snapshot-vm="labs-demo"' in body
    assert "submitSnapshot(event" not in body
    # The JSON endpoint must not receive form-encoded data from an HTMX form.
    assert 'hx-post="/api/v1/vms/labs-demo/snapshots"' not in body


def test_vm_status_partial_includes_actions(web, monkeypatch, make_fake_client):
    running = make_fake_client(vms=[VMInfo(name="labs-run", state="running")])
    monkeypatch.setattr(ui, "get_host_client", lambda: running)
    body = web.get("/partials/vm-status/labs-run").text
    assert "Stop" in body and "Reboot" in body

    stopped = make_fake_client(vms=[VMInfo(name="labs-off", state="shut off")])
    monkeypatch.setattr(ui, "get_host_client", lambda: stopped)
    body = web.get("/partials/vm-status/labs-off").text
    assert "Start" in body
    assert "/stop" not in body
    assert "Reboot" not in body


def test_vm_console_buttons_follow_state(web, monkeypatch, make_fake_client):
    stopped = make_fake_client(vms=[VMInfo(name="labs-off", state="shut off")])
    monkeypatch.setattr(ui, "get_host_client", lambda: stopped)
    body = web.get("/partials/vm-console/labs-off").text
    assert "Serial console" in body and "disabled" in body

    running = make_fake_client(vms=[VMInfo(name="labs-run", state="running")])
    monkeypatch.setattr(ui, "get_host_client", lambda: running)
    body = web.get("/partials/vm-console/labs-run").text
    assert "disabled" not in body


def test_vm_detail_polls_actions_and_console(web, monkeypatch, fake_client):
    monkeypatch.setattr(ui, "get_host_client", lambda: fake_client)
    body = web.get("/vms/labs-demo").text
    assert 'hx-get="/partials/vm-status/labs-demo"' in body
    assert 'hx-get="/partials/vm-console/labs-demo"' in body
