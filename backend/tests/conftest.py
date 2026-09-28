"""Shared pytest fixtures.

Runs with *strict* Jinja2 rendering so a missing template variable fails the
test instead of silently rendering an empty dashboard (the bug that produced
``'cpu_percent' is undefined``).
"""
import os
import re
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# Configure settings BEFORE the app is imported. Pin every deployment-mode
# variable so an exported LOCAL_AGENT/HOST_MODE cannot spawn an in-process agent
# or hit a real network during tests.
os.environ.setdefault("TEMPLATES_DIR", str(BACKEND_DIR / "cloud_init_templates"))
os.environ["MODE"] = "control"
os.environ["HOST_MODE"] = "local"
os.environ["LOCAL_AGENT"] = "false"
os.environ.pop("AGENT_URL", None)
os.environ.pop("AGENT_TOKEN", None)
# The background guest-metrics sampler must not run during tests; the collector
# is exercised directly instead.
os.environ.setdefault("METRICS_ENABLED", "false")
if not os.environ.get("VM_STORAGE_PATH"):
    _libvirt_images = Path("/var/lib/libvirt/images")
    os.environ["VM_STORAGE_PATH"] = str(
        _libvirt_images if _libvirt_images.is_dir() else Path(tempfile.gettempdir())
    )

import pytest  # noqa: E402
from jinja2 import StrictUndefined  # noqa: E402

from app import ui  # noqa: E402
from app.host import HostUsage  # noqa: E402
from app.virsh_client import VMInfo, TemplateInfo  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def strict_jinja_undefined():
    """Every template rendered during tests must define all its variables."""
    previous = ui.templates.env.undefined
    ui.templates.env.undefined = StrictUndefined
    yield
    ui.templates.env.undefined = previous


@pytest.fixture
def sample_host_usage() -> HostUsage:
    return HostUsage(
        cpu_percent=14.2,
        mem_total_mb=29760,
        mem_available_mb=9469,
        mem_used_mb=20291,
        mem_percent=68.2,
        load_1m=0.97,
        load_5m=0.92,
        load_15m=0.85,
        cpu_cores=16,
        disk_total_gb=465,
        disk_available_gb=99,
        disk_used_gb=366,
        disk_percent=78.2,
    )


@pytest.fixture
def sample_vms() -> list[VMInfo]:
    return [
        VMInfo(
            name="labs-demo",
            state="running",
            ip="192.168.122.10",
            tailscale_ip="100.101.102.103",
            vcpus=2,
            memory_mb=2048,
            disk_gb=20,
            snapshots=["clean"],
        ),
        VMInfo(name="labs-vm2", state="shut off", ip=None,
               vcpus=2, memory_mb=2048, disk_gb=20, snapshots=[]),
    ]


@pytest.fixture
def sample_templates() -> list[TemplateInfo]:
    return [
        TemplateInfo(
            name="rhel-10",
            path="/images/rhel-10.qcow2",
            description="RHEL 10.2 cloud image",
            memory_mb=2048,
            vcpus=2,
            disk_gb=20,
        ),
        TemplateInfo(
            name="fedora-44",
            path="/images/fedora-44.qcow2",
            description="Fedora 44 cloud image",
            memory_mb=2048,
            vcpus=2,
            disk_gb=20,
        ),
    ]


class FakeVirshClient:
    """In-memory stand-in so UI/API tests never touch a real hypervisor."""

    def __init__(self, vms=None, templates=None):
        self.vms = list(vms or [])
        self.templates = list(templates or [])
        self.create_calls: list[dict] = []
        self.calls: list[tuple] = []
        self.snapshots: dict[str, list[str]] = {}
        self.display = ("127.0.0.1", 5900)
        self.fail = False
        self.tailscale_ok = False

    # -- reads
    def list_vms(self):
        return list(self.vms)

    def list_templates(self):
        return list(self.templates)

    def get_vm(self, name):
        return next((v for v in self.vms if v.name == name), None)

    def get_template(self, name):
        return next((t for t in self.templates if t.name == name), None)

    def list_snapshots(self, name):
        return list(self.snapshots.get(name, []))

    def get_display(self, name):
        return self.display

    def tailscale_available(self):
        return self.tailscale_ok

    def ping(self):
        return True

    def get_vm_user(self, name):
        return getattr(self, "vm_user", "cloud-user")

    def get_console_command(self, name):
        return ["virsh", "-c", "qemu:///system", "console", name]

    def get_vm_memory_stats(self, name):
        return getattr(self, "balloon", None)

    def agent_command(self, name, execute, arguments=None, timeout=15):
        return getattr(self, "agent_responses", {}).get(execute)

    def guest_exec_capture(self, name, script, timeout=6):
        return getattr(self, "guest_exec", None)

    def list_imported_images(self):
        return list(getattr(self, "images", []))

    def inspect_image(self, path):
        return {"format": "qcow2", "disk_gb": 10} if getattr(self, "accept_uploads", True) else None

    def resolve_image(self, ref):
        base = getattr(self, "import_base", None)
        if base is None:
            raise ValueError(f"Image '{ref}' not found")
        path = base / ref
        if not path.is_file():
            raise ValueError(f"Image '{ref}' not found")
        return path

    def delete_imported_image(self, name):
        from app.virsh_client import VirshResult
        self.calls.append(("delete_imported_image", name))
        if self.fail:
            return VirshResult(success=False, stderr="boom")
        # Resolve the image path and delete it from disk (as the real client does)
        try:
            path = self.resolve_image(name)
        except ValueError:
            return VirshResult(success=False, stderr=f"Image '{name}' not found")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return VirshResult(success=False, stderr=f"Could not delete image: {name}")
        return VirshResult(success=True, stdout=f"Image '{name}' deleted")

    def upload_image(self, filename, stream, content_type=None):
        """Remote-mode upload; the fake just records the name."""
        self.calls.append(("upload_image", filename))
        return {"name": filename, "format": "qcow2", "disk_gb": 10}

    # -- helpers
    def provision_name(self, name, template=None):
        def slug(value):
            return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")

        raw = (name or "").strip()
        slugged = slug(raw)
        if raw and raw.lower().startswith("labs-") and slugged:
            return slugged
        token = ""
        if template:
            token = slug(template.rsplit(".", 1)[0] if "." in template else template)
        base = f"{token}-{slugged}" if token else slugged
        candidate = f"labs-{base}"
        if len(candidate) > 63 or not re.fullmatch(r"labs-[a-z0-9]+(?:-[a-z0-9]+)*", candidate):
            raise ValueError("Enter a name with at least one letter or digit")
        return candidate

    def vm_exists(self, name):
        return any(v.name == name for v in self.vms)

    def _result(self, *args):
        from app.virsh_client import VirshResult

        self.calls.append(args)
        if self.fail:
            return VirshResult(success=False, stderr="boom")
        return VirshResult(success=True, stdout="ok")

    # -- mutations
    def create_vm(self, **kwargs):
        from app.virsh_client import VirshResult

        self.create_calls.append(kwargs)
        return VirshResult(success=True, stdout=f"VM {kwargs.get('name')} created")

    def start_vm(self, name):
        return self._result("start", name)

    def stop_vm(self, name):
        return self._result("stop", name)

    def reboot_vm(self, name):
        return self._result("reboot", name)

    def force_stop_vm(self, name):
        return self._result("destroy", name)

    def delete_vm(self, name):
        return self._result("delete", name)

    def create_snapshot(self, name, snapshot):
        return self._result("snapshot-create", name, snapshot)

    def revert_snapshot(self, name, snapshot):
        return self._result("snapshot-revert", name, snapshot)

    def delete_snapshot(self, name, snapshot):
        return self._result("snapshot-delete", name, snapshot)


@pytest.fixture
def fake_client(sample_vms, sample_templates) -> FakeVirshClient:
    return FakeVirshClient(vms=sample_vms, templates=sample_templates)


@pytest.fixture
def make_fake_client():
    """Factory for ad-hoc fake clients inside a test."""

    def _make(vms=None, templates=None) -> FakeVirshClient:
        return FakeVirshClient(vms=vms, templates=templates)

    return _make


@pytest.fixture(autouse=True)
def reset_console_sessions():
    """Keep the module-global console session counter from leaking between tests."""
    from app import console
    console._active_sessions.clear()
    yield
    console._active_sessions.clear()


@pytest.fixture
def web_client(monkeypatch):
    """A TestClient with the host client swapped for a fake."""
    from fastapi.testclient import TestClient
    from app.main import app

    def _make(fake):
        import app.api.v1 as api_v1
        import app.ws_utils as ws_utils
        monkeypatch.setattr(api_v1, "get_host_client", lambda: fake)
        monkeypatch.setattr(ws_utils, "get_host_client", lambda: fake)
        return TestClient(app)

    return _make