"""Unit tests for the VirshClient core logic (all subprocess calls mocked)."""
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import settings
from app.virsh_client import VMInfo, VirshClient, VirshResult, _is_tailscale_ip

_SEED_TOOLS = ("cloud-localds", "genisoimage", "xorriso", "mkisofs")


def _file_from_cmd(cmd, name):
    for arg in cmd:
        if str(arg).endswith(name):
            return Path(arg)
    return None


def _client(**kwargs) -> VirshClient:
    return VirshClient(**kwargs)


# ---------------------------------------------------------------- helpers

@pytest.mark.parametrize("addr,expected", [
    ("100.101.102.103", True),
    ("100.64.0.1", True),
    ("100.127.255.254", True),
    ("100.63.255.255", False),
    ("192.168.122.10", False),
    ("not-an-ip", False),
    ("", False),
])
def test_is_tailscale_ip(addr, expected):
    assert _is_tailscale_ip(addr) is expected


@pytest.mark.parametrize("state,expected", [
    ("running", "status-running"),
    ("shut off", "status-stopped"),
    ("paused", "status-paused"),
    ("in shutdown", "status-stopping"),
    ("weird", "status-unknown"),
])
def test_status_class(state, expected):
    assert VMInfo(name="labs-x", state=state).status_class == expected


# ---------------------------------------------------------------- _run / _run_raw

def test_run_propagates_failure():
    client = _client()
    with patch("app.virsh_client.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, "", "virsh: boom")
        result = client._run(["domstate", "labs-x"])
    assert result.success is False
    assert result.stderr == "virsh: boom"
    assert result.returncode == 1


def test_run_handles_timeout():
    client = _client()
    with patch("app.virsh_client.subprocess.run", side_effect=subprocess.TimeoutExpired([], 1)):
        result = client._run(["domstate", "labs-x"])
    assert result.success is False
    assert "timed out" in result.stderr.lower()


def test_run_raw_handles_exception():
    client = _client()
    with patch("app.virsh_client.subprocess.run", side_effect=OSError("no binary")):
        result = client._run_raw(["qemu-img", "create", "x"])
    assert result.success is False
    assert "no binary" in result.stderr


def test_run_guards_non_lab_target():
    client = _client()
    with patch("app.virsh_client.subprocess.run") as run:
        with pytest.raises(PermissionError):
            client._run(["domstate", "fedora"])
        run.assert_not_called()


# ---------------------------------------------------------------- listing / status

def test_list_vms_returns_empty_on_failure():
    client = _client()
    with patch.object(client, "_run", return_value=VirshResult(False, stderr="down")):
        assert client.list_vms() == []


def test_get_vm_addresses_empty_on_failure():
    client = _client()
    with patch.object(client, "_run", return_value=VirshResult(False)):
        assert client.get_vm_addresses("labs-x") == []


def test_vm_exists_true_and_false():
    client = _client()
    with patch.object(client, "_run", return_value=VirshResult(True, stdout="shut off")):
        assert client.vm_exists("labs-x") is True
    with patch.object(client, "_run", return_value=VirshResult(False)):
        assert client.vm_exists("labs-x") is False


def test_get_display_bad_xml_returns_none():
    client = _client()
    with patch.object(client, "_run", return_value=VirshResult(True, stdout="<not xml")):
        assert client.get_display("labs-x") is None


# ---------------------------------------------------------------- lifecycle

def test_lifecycle_command_mapping():
    client = _client()
    with patch.object(client, "_run", return_value=VirshResult(True)) as run:
        client.start_vm("labs-x")
        client.stop_vm("labs-x")
        client.force_stop_vm("labs-x")
        client.reboot_vm("labs-x")
    verbs = [call.args[0][0] for call in run.call_args_list]
    assert verbs == ["start", "shutdown", "destroy", "reboot"]


def test_delete_vm_happy_path():
    client = _client()
    calls = []

    def fake(args):
        calls.append(args)
        if args[0] == "snapshot-list":
            return VirshResult(True, stdout="snap1\nsnap2")
        return VirshResult(True)

    with patch.object(client, "_run", side_effect=fake):
        result = client.delete_vm("labs-x")

    assert result.success
    assert ["destroy", "labs-x"] in calls
    assert ["snapshot-delete", "labs-x", "snap1"] in calls
    assert ["snapshot-delete", "labs-x", "snap2"] in calls
    assert ["undefine", "labs-x", "--nvram", "--remove-all-storage"] in calls


def test_delete_vm_falls_back_to_manual_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        (storage / "labs-x.qcow2").write_text("disk")
        (storage / "seed-labs-x.iso").write_text("seed")
        client = _client(storage_path=str(storage))
        calls = []

        def fake(args):
            calls.append(args)
            if args[0] == "snapshot-list":
                return VirshResult(True, stdout="")
            if args[0] == "undefine" and "--remove-all-storage" in args:
                return VirshResult(False, stderr="cannot undefine")
            return VirshResult(True)

        with patch.object(client, "_run", side_effect=fake):
            result = client.delete_vm("labs-x")

        # The primary undefine failed but the fallback + manual cleanup succeeded.
        assert result.success is True
        assert ["undefine", "labs-x"] in calls
        assert not (storage / "labs-x.qcow2").exists()
        assert not (storage / "seed-labs-x.iso").exists()


def _tailnet_meta_run(state: str):
    """A fake `_run` reporting a Tailscale-enabled VM in the given state."""
    def fake(args):
        if args[0] == "desc":
            return VirshResult(True, stdout="labforge:user=debian labforge:tailscale=1")
        if args[0] == "domstate":
            return VirshResult(True, stdout=state)
        return VirshResult(True)
    return fake


def test_delete_vm_logs_out_tailscale_first():
    client = _client()

    def fake(args):
        if args[0] == "snapshot-list":
            return VirshResult(True, stdout="")
        return VirshResult(True)

    with patch.object(client, "_run", side_effect=fake), \
         patch.object(client, "logout_tailscale") as logout:
        client.delete_vm("labs-x")

    logout.assert_called_once_with("labs-x")


def test_logout_tailscale_runs_for_running_vm():
    client = _client()
    with patch.object(client, "_run", side_effect=_tailnet_meta_run("running")), \
         patch.object(client, "_guest_exec", return_value=True) as guest_exec:
        assert client.logout_tailscale("labs-x") is True

    guest_exec.assert_called_once()
    assert "tailscale logout" in guest_exec.call_args.args[1]


def test_logout_tailscale_skips_stopped_vm():
    client = _client()
    with patch.object(client, "_run", side_effect=_tailnet_meta_run("shut off")), \
         patch.object(client, "_guest_exec") as guest_exec:
        assert client.logout_tailscale("labs-x") is False

    guest_exec.assert_not_called()


def test_logout_tailscale_skips_vm_without_tailscale():
    client = _client()

    def fake(args):
        if args[0] == "desc":
            return VirshResult(True, stdout="labforge:user=debian labforge:tailscale=0")
        return VirshResult(True, stdout="running")

    with patch.object(client, "_run", side_effect=fake), \
         patch.object(client, "_guest_exec") as guest_exec:
        assert client.logout_tailscale("labs-x") is False

    guest_exec.assert_not_called()


def test_logout_tailscale_skipped_when_disabled():
    client = _client()
    with patch.object(settings, "tailscale_logout_on_delete", False), \
         patch.object(client, "_guest_exec") as guest_exec:
        assert client.logout_tailscale("labs-x") is False

    guest_exec.assert_not_called()


def test_guest_exec_polls_until_exit():
    client = _client()
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        if '"guest-exec-status"' in args[2]:
            done = sum('"guest-exec-status"' in c[2] for c in calls) >= 2
            return VirshResult(True, stdout='{"return": {"exited": %s}}' % str(done).lower())
        if '"guest-exec"' in args[2]:
            return VirshResult(True, stdout='{"return": {"pid": 7}}')
        return VirshResult(True)

    with patch.object(client, "_run", side_effect=fake), \
         patch("app.virsh_client.time.sleep"):
        assert client._guest_exec("labs-x", "true") is True

    assert any('"guest-exec-status"' in c[2] for c in calls)


def test_guest_exec_false_when_agent_unavailable():
    client = _client()
    with patch.object(client, "_run", return_value=VirshResult(False, stderr="no agent")):
        assert client._guest_exec("labs-x", "true") is False


# ---------------------------------------------------------------- templates

def test_list_templates_and_get_template():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = root / "rhel-10"
        good.mkdir()
        (good / "base.qcow2").write_bytes(b"img")
        (good / "template.json").write_text(
            '{"description":"RHEL","memory_mb":4096,"vcpus":4,"disk_gb":40,"ssh_user":"cloud-user"}'
        )
        (root / "empty").mkdir()          # no qcow2 -> ignored
        (root / "no-image").mkdir()
        (root / "no-image" / "template.json").write_text("{}")

        client = _client(templates_dir=str(root))
        templates = client.list_templates()
        assert [t.name for t in templates] == ["rhel-10"]
        tpl = client.get_template("rhel-10")
        assert tpl.memory_mb == 4096 and tpl.vcpus == 4 and tpl.disk_gb == 40
        # path traversal / weird names are rejected
        assert client.get_template("../etc") is None
        assert client.get_template("does-not-exist") is None


# ---------------------------------------------------------------- create_vm guards

def _templates_with_default(tmp: Path) -> Path:
    root = tmp / "templates"
    default = root / "default"
    default.mkdir(parents=True)
    src = Path(__file__).resolve().parent.parent / "cloud_init_templates" / "default"
    for f in src.iterdir():
        (default / f.name).write_text(f.read_text())
    tpl = root / "testos"
    tpl.mkdir()
    (tpl / "base.qcow2").write_bytes(b"img")
    (tpl / "template.json").write_text(
        '{"memory_mb":2048,"vcpus":2,"disk_gb":20,"os_variant":"generic","ssh_user":"cloud-user"}'
    )
    return root


def test_create_vm_rejects_unknown_template():
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(templates_dir=_templates_with_default(Path(tmp)),
                         storage_path=tmp)
        with patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]):
            result = client.create_vm("demo", "nope")
        assert result.success is False
        assert "not found" in result.stderr.lower()


def test_create_vm_rejects_insufficient_resources():
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(templates_dir=_templates_with_default(Path(tmp)),
                         storage_path=tmp)
        with patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch("app.virsh_client.check_resources",
                   return_value={"ok": False, "reasons": ["not enough RAM"]}):
            result = client.create_vm("demo", "testos")
        assert result.success is False
        assert "insufficient" in result.stderr.lower()


def test_create_vm_requires_ssh_key_file():
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(templates_dir=_templates_with_default(Path(tmp)),
                         storage_path=tmp)
        with patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
             patch.object(settings, "ssh_public_keys_file", None):
            result = client.create_vm("demo", "testos")
        assert result.success is False
        assert "SSH_PUBLIC_KEYS_FILE" in result.stderr


def test_create_vm_rejects_invalid_ssh_keys():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        client = _client(templates_dir=_templates_with_default(root), storage_path=str(root))
        bad = root / "bad.pub"
        bad.write_text("this is not a key\n")
        with patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
             patch.object(settings, "ssh_public_keys_file", str(bad)):
            result = client.create_vm("demo", "testos")
        assert result.success is False
        assert "must contain public SSH keys" in result.stderr


def test_create_vm_refuses_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage = root / "storage"
        storage.mkdir()
        (storage / "labs-testos-demo.qcow2").write_bytes(b"existing")
        client = _client(templates_dir=_templates_with_default(root), storage_path=str(storage))
        good = root / "good.pub"
        good.write_text("ssh-ed25519 AAAA test\n")
        with patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
             patch.object(settings, "ssh_public_keys_file", str(good)):
            result = client.create_vm("demo", "testos")
        assert result.success is False
        assert "refusing overwrite" in result.stderr.lower()


def test_create_vm_rejects_unwritable_storage():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = root / "missing-storage"
        client = _client(templates_dir=_templates_with_default(root), storage_path=str(missing))
        good = root / "good.pub"
        good.write_text("ssh-ed25519 AAAA test\n")
        with patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
             patch.object(settings, "ssh_public_keys_file", str(good)):
            result = client.create_vm("demo", "testos")
        assert result.success is False
        assert "writable" in result.stderr.lower()
        assert str(missing) in result.stderr


def test_get_vm_spec_reads_dominfo_and_disk():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        (storage / "labs-x.qcow2").write_bytes(b"img")
        client = _client(storage_path=str(storage))
        dominfo = (
            "Id:             1\n"
            "Name:           labs-x\n"
            "CPU(s):         4\n"
            "Max memory:     4194304 KiB\n"
            "Used memory:    4194304 KiB\n"
        )

        def fake_run(args):
            return VirshResult(True, dominfo) if args[0] == "dominfo" else VirshResult(True)

        captured = {}

        def fake_raw(cmd):
            captured["cmd"] = cmd
            return VirshResult(True, '{"virtual-size": 21474836480}')

        with patch.object(client, "_run", side_effect=fake_run), \
             patch.object(client, "_run_raw", side_effect=fake_raw):
            spec = client.get_vm_spec("labs-x")

        assert spec == {"vcpus": 4, "memory_mb": 4096, "disk_gb": 20}
        # Running VMs hold a write lock, so metadata must be read with --force-share.
        assert "-U" in captured["cmd"]


def test_list_vms_includes_spec_and_user():
    client = _client()

    def fake(args):
        return {
            "list": VirshResult(True, "labs-a"),
            "domstate": VirshResult(True, "running"),
            "domifaddr": VirshResult(True, ""),
            "snapshot-list": VirshResult(True, ""),
            "desc": VirshResult(True, "labforge:user=deploy"),
            "dominfo": VirshResult(True, "CPU(s): 2\nMax memory: 2097152 KiB\n"),
        }.get(args[0], VirshResult(True))

    with patch.object(client, "_run", side_effect=fake):
        vms = client.list_vms()

    assert len(vms) == 1
    vm = vms[0]
    assert vm.user == "deploy"
    assert vm.vcpus == 2
    assert vm.memory_mb == 2048


def test_get_vm_user_parses_description():
    client = _client()
    with patch.object(client, "_run",
                      return_value=VirshResult(True, "notes\nlabforge:user=deploy\nmore")):
        assert client.get_vm_user("labs-x") == "deploy"
    with patch.object(client, "_run", return_value=VirshResult(True, "no metadata here")):
        assert client.get_vm_user("labs-x") is None
    with patch.object(client, "_run", return_value=VirshResult(False)):
        assert client.get_vm_user("labs-x") is None


def test_get_vm_meta_parses_user_and_tailscale():
    client = _client()
    desc = "labforge:user=deploy labforge:tailscale=1"
    with patch.object(client, "_run", return_value=VirshResult(True, desc)):
        meta = client._get_vm_meta("labs-x")
    assert meta == {"user": "deploy", "tailscale": "1"}


def test_get_vm_memory_stats_parses_dommemstat():
    client = _client()
    out = ("actual 1572864\n"
           "swap_in 0\n"
           "unused 1222588\n"
           "available 1492652\n"
           "last_update 1790442377\n")
    with patch.object(client, "_run", return_value=VirshResult(True, out)) as run:
        stats = client.get_vm_memory_stats("labs-x")
    run.assert_called_once_with(["dommemstat", "labs-x"])
    assert stats == {"actual": 1572864, "swap_in": 0, "unused": 1222588,
                     "available": 1492652, "last_update": 1790442377}

    with patch.object(client, "_run", return_value=VirshResult(False)):
        assert client.get_vm_memory_stats("labs-x") is None


def test_build_vm_info_sets_tailscale_enabled():
    client = _client()
    desc = "labforge:user=deploy labforge:tailscale=1"

    def fake(args):
        if args[0] == "desc":
            return VirshResult(True, desc)
        if args[0] == "domstate":
            return VirshResult(True, "running")
        return VirshResult(True, "")

    with patch.object(client, "_run", side_effect=fake):
        info = client._build_vm_info("labs-x")
    assert info.tailscale_enabled is True
    assert info.user == "deploy"


def test_tailscale_available_requires_key_file_and_tool(tmp_path):
    client = _client()
    key = tmp_path / "auth"
    key.write_text("dummy\n")
    with patch.object(settings, "tailscale_auth_key_file", str(key)), \
         patch.object(client, "_find_iso_tool", return_value="genisoimage"):
        assert client.tailscale_available() is True
    with patch.object(settings, "tailscale_auth_key_file", str(key)), \
         patch.object(client, "_find_iso_tool", return_value=None):
        assert client.tailscale_available() is False
    with patch.object(settings, "tailscale_auth_key_file", None):
        assert client.tailscale_available() is False


def test_create_vm_injects_credentials_and_records_user():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage = root / "storage"
        storage.mkdir()
        client = _client(templates_dir=_templates_with_default(root), storage_path=str(storage))
        key = root / "good.pub"
        key.write_text("ssh-ed25519 AAAA test\n")
        seeds, installs = [], []

        def raw(cmd):
            if cmd[0] in _SEED_TOOLS:
                data = _file_from_cmd(cmd, "user-data").read_text()
                seeds.append(data)
            elif cmd[0] == "virt-install" and "--name" in cmd:
                installs.append(cmd)
            return VirshResult(True)

        with patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
             patch.object(settings, "ssh_public_keys_file", str(key)), \
             patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch.object(client, "_run_raw", side_effect=raw), \
             patch.object(client, "_find_iso_tool", return_value="genisoimage"), \
             patch.object(client, "get_vm_ip", return_value="192.0.2.10"):
            result = client.create_vm("demo", "testos", vm_user="deploy", vm_password="pw-123")

        assert result.success, result.stderr
        install = installs[0]
        description = install[install.index("--description") + 1]
        assert description.startswith("labforge:user=deploy")
        assert "labforge:tailscale=" in description
        assert any("deploy" in s and "pw-123" in s for s in seeds)


def test_create_vm_enabled_tailscale_reaches_cloud_init():
    """The auth key file is byte-copied onto the seed ISO."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage = root / "storage"
        storage.mkdir()
        client = _client(templates_dir=_templates_with_default(root), storage_path=str(storage))
        key = root / "good.pub"
        key.write_text("ssh-ed25519 AAAA test\n")
        ts_file = root / "auth"
        ts_file.write_text("tskey-abc\n")
        seeds = []

        def raw(cmd):
            if cmd[0] in _SEED_TOOLS:
                for a in cmd:
                    if isinstance(a, str) and a.endswith("authkey"):
                        seeds.append(Path(a).read_text())
                    if isinstance(a, str) and a.endswith("user-data"):
                        seeds.append(Path(a).read_text())
            return VirshResult(True)

        with patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
             patch.object(settings, "ssh_public_keys_file", str(key)), \
             patch.object(settings, "tailscale_auth_key_file", str(ts_file)), \
             patch.object(client, "vm_exists", return_value=False), \
             patch.object(client, "list_vms", return_value=[]), \
             patch.object(client, "_run_raw", side_effect=raw), \
             patch.object(client, "_find_iso_tool", return_value="genisoimage"), \
             patch.object(client, "get_vm_ip", return_value="192.0.2.10"):
            result = client.create_vm("demo", "testos", enable_tailscale=True)

        assert result.success, result.stderr
        assert any("tskey-abc" in s for s in seeds)
        # --authkey is cloud-localds specific; genisoimage just takes file paths


# ---------------------------------------------------------------- uploaded images

def test_list_imported_images(tmp_path, monkeypatch):
    (tmp_path / "ubuntu-24.04.qcow2").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignore me")
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    client = _client()
    info = '{"format":"qcow2","virtual-size":21474836480}'
    with patch.object(client, "_run_raw", return_value=VirshResult(True, info)):
        images = client.list_imported_images()
    assert [i["name"] for i in images] == ["ubuntu-24.04.qcow2"]
    assert images[0]["disk_gb"] == 20


def test_resolve_image_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    (tmp_path / "ok.qcow2").write_bytes(b"x")
    client = _client()
    assert client.resolve_image("ok.qcow2").name == "ok.qcow2"
    for bad in ("../etc/passwd", "/etc/passwd", "..", "sub/ok.qcow2", ""):
        with pytest.raises(ValueError):
            client.resolve_image(bad)
    with pytest.raises(ValueError):
        client.resolve_image("missing.qcow2")


def test_inspect_image_requires_qcow2(tmp_path):
    path = tmp_path / "x.qcow2"
    path.write_bytes(b"x")
    client = _client()
    with patch.object(client, "_run_raw",
                      return_value=VirshResult(True, '{"format":"qcow2","virtual-size":10737418240}')):
        info = client.inspect_image(path)
        assert info["format"] == "qcow2"
        assert info["disk_gb"] == 10
    with patch.object(client, "_run_raw",
                      return_value=VirshResult(True, '{"format":"raw","virtual-size":10737418240}')):
        assert client.inspect_image(path) is None
    with patch.object(client, "_run_raw", return_value=VirshResult(False, stderr="nope")):
        assert client.inspect_image(path) is None


def test_create_vm_from_uploaded_image(tmp_path, monkeypatch):
    templates = _templates_with_default(tmp_path)
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    image = import_dir / "ubuntu-24.04.qcow2"
    image.write_bytes(b"fake base image")
    storage = tmp_path / "storage"
    storage.mkdir()
    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAA test\n")

    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    client = _client(templates_dir=str(templates), storage_path=str(storage))
    installs = []

    def raw(cmd):
        if cmd[0] == "qemu-img" and "info" in cmd:
            return VirshResult(True, '{"format":"qcow2","virtual-size":21474836480}')
        if cmd[0] == "virt-install" and "--name" in cmd:
            installs.append(cmd)
        return VirshResult(True)

    with patch("app.virsh_client.check_resources", return_value={"ok": True, "reasons": []}), \
         patch.object(settings, "ssh_public_keys_file", str(key)), \
         patch.object(settings, "tailscale_auth_key_file", None), \
         patch.object(client, "vm_exists", return_value=False), \
         patch.object(client, "list_vms", return_value=[]), \
         patch.object(client, "_run_raw", side_effect=raw), \
         patch.object(client, "_find_iso_tool", return_value="genisoimage"), \
         patch.object(client, "get_vm_ip", return_value="192.0.2.10"):
        result = client.create_vm("demo", image="ubuntu-24.04.qcow2")

    assert result.success, result.stderr
    assert installs, "virt-install was not called"
    install = installs[0]
    assert install[install.index("--os-variant") + 1] == "ubuntu24.04"
    assert (storage / "labs-ubuntu-24-04-demo.qcow2").read_bytes() == b"fake base image"


def test_create_vm_rejects_disk_smaller_than_image(tmp_path, monkeypatch):
    templates = _templates_with_default(tmp_path)
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    (import_dir / "big.qcow2").write_bytes(b"x")
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    client = _client(templates_dir=str(templates), storage_path=str(storage))

    with patch.object(client, "vm_exists", return_value=False), \
         patch.object(client, "_run_raw",
                      return_value=VirshResult(True, '{"format":"qcow2","virtual-size":42949672960}')):
        result = client.create_vm("demo", image="big.qcow2", disk_gb=10)
    assert result.success is False
    assert "cannot be shrunk" in result.stderr


# ---------------------------------------------------------------- image validation

def test_resolve_image_rejects_symlink_escape(tmp_path, monkeypatch):
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    outside = tmp_path / "secret.qcow2"
    outside.write_bytes(b"x")
    (import_dir / "link.qcow2").symlink_to(outside)
    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    with pytest.raises(ValueError):
        _client().resolve_image("link.qcow2")


def test_resolve_image_rejects_directory(tmp_path, monkeypatch):
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    (import_dir / "adir.qcow2").mkdir()
    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    with pytest.raises(ValueError):
        _client().resolve_image("adir.qcow2")


def test_resolve_image_rejects_absolute_outside(tmp_path, monkeypatch):
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    (import_dir / "ok.qcow2").write_bytes(b"x")
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"x")
    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    with pytest.raises(ValueError):
        _client().resolve_image(str(outside))


def test_resolve_image_rejects_vm_disk(tmp_path, monkeypatch):
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    (import_dir / "labs-live.qcow2").write_bytes(b"x")
    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    with pytest.raises(ValueError):
        _client().resolve_image("labs-live.qcow2")


def test_list_imported_images_hides_temp_and_vm_disks(tmp_path, monkeypatch):
    (tmp_path / "good.qcow2").write_bytes(b"x")
    (tmp_path / ".in-flight.qcow2.part").write_bytes(b"x")
    (tmp_path / "labs-live.qcow2").write_bytes(b"x")
    monkeypatch.setattr(settings, "import_dir", str(tmp_path))
    client = _client()
    info = '{"format":"qcow2","virtual-size":10737418240}'
    with patch.object(client, "_run_raw", return_value=VirshResult(True, info)):
        names = [i["name"] for i in client.list_imported_images()]
    assert names == ["good.qcow2"]


def test_virtual_size_gb_rounds_up(tmp_path):
    path = tmp_path / "x.qcow2"
    path.write_bytes(b"x")
    size = 21 * 1024 ** 3 + 123456
    client = _client()
    with patch.object(client, "_run_raw",
                      return_value=VirshResult(True, '{"format":"qcow2","virtual-size":%d}' % size)):
        assert client._virtual_size_gb(path) == 22


# ---------------------------------------------------------------- os variant

def test_resolve_os_variant_prefers_family_then_generic():
    client = _client()
    supported = {"fedora43", "fedora42", "ubuntu23.10", "rhel9.7", "centos-stream10"}
    with patch.object(client, "_supported_os_variants", return_value=supported):
        assert client._resolve_os_variant("fedora44") == "fedora43"
        assert client._resolve_os_variant("ubuntu24.04") == "ubuntu23.10"
        assert client._resolve_os_variant("rhel10") == "rhel9.7"
        assert client._resolve_os_variant("centos-stream10") == "centos-stream10"
        assert client._resolve_os_variant("weird99") == "generic"
        assert client._resolve_os_variant("") == "generic"


# ---------------------------------------------------------------- create_vm guards

def test_create_vm_requires_template_or_image():
    client = _client()
    with patch.object(client, "vm_exists", return_value=False):
        result = client.create_vm("demo")
    assert result.success is False
    assert "template" in result.stderr.lower() or "image" in result.stderr.lower()


def test_create_vm_disk_equal_and_one_gb_smaller(tmp_path, monkeypatch):
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    (import_dir / "img.qcow2").write_bytes(b"x")
    monkeypatch.setattr(settings, "import_dir", str(import_dir))
    client = _client(templates_dir=_templates_with_default(tmp_path),
                     storage_path=str(tmp_path / "storage"))
    (tmp_path / "storage").mkdir(exist_ok=True)
    info = '{"format":"qcow2","virtual-size":%d}' % (20 * 1024 ** 3)

    with patch.object(client, "vm_exists", return_value=False), \
         patch.object(client, "_run_raw", return_value=VirshResult(True, info)), \
         patch.object(settings, "ssh_public_keys_file", None):
        equal = client.create_vm("demo", image="img.qcow2", disk_gb=20)
    assert "cannot be shrunk" not in equal.stderr

    with patch.object(client, "vm_exists", return_value=False), \
         patch.object(client, "_run_raw", return_value=VirshResult(True, info)), \
         patch.object(settings, "ssh_public_keys_file", None):
        smaller = client.create_vm("demo", image="img.qcow2", disk_gb=19)
    assert "cannot be shrunk" in smaller.stderr
