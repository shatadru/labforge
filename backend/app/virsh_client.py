"""Virsh client - wraps virsh/qemu-img/cloud-localds subprocess calls.

Reuses logic from ~/myhomelab/playbooks/tasks/create-vm-instance.yaml
and ~/myhomelab/playbooks/delete-vm.yaml
"""
import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import settings
from app.host import check_resources

try:  # jinja2 ships with the app; keep the import close to its use site
    from jinja2 import TemplateError
except ImportError:  # pragma: no cover - jinja2 is a hard dependency
    TemplateError = Exception

# Tailscale hands out addresses from the CGNAT range 100.64.0.0/10.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# Accepted image file extensions for templates and uploads.
IMAGE_SUFFIXES = (".qcow2", ".qcow", ".img", ".raw")

# Serialises provisioning so the budget check and disk creation are atomic.
_create_lock = threading.Lock()


def _is_tailscale_ip(addr: str) -> bool:
    """True if addr belongs to Tailscale's 100.64.0.0/10 range."""
    try:
        return ipaddress.ip_address(addr) in _TAILSCALE_NET
    except ValueError:
        return False


@dataclass
class VMInfo:
    name: str
    state: str
    ip: Optional[str] = None
    tailscale_ip: Optional[str] = None
    user: Optional[str] = None
    tailscale_enabled: bool = False
    vcpus: int = 0
    memory_mb: int = 0
    disk_gb: int = 0
    snapshots: list[str] = None

    def __post_init__(self):
        if self.snapshots is None:
            self.snapshots = []

    @property
    def status_class(self) -> str:
        return {
            "running": "status-running",
            "shut off": "status-stopped",
            "paused": "status-paused",
            "in shutdown": "status-stopping",
        }.get(self.state.lower(), "status-unknown")

    @property
    def state_label(self) -> str:
        """Human-readable state for the UI ('shut off' reads as 'Stopped')."""
        return {
            "running": "Running",
            "shut off": "Stopped",
            "in shutdown": "Stopping",
            "paused": "Paused",
            "pmsuspended": "Suspended",
            "crashed": "Crashed",
        }.get(self.state.lower(), self.state)


@dataclass
class TemplateInfo:
    name: str
    path: str
    description: str = ""
    memory_mb: int = 2048
    vcpus: int = 2
    disk_gb: int = 20
    os_variant: str = "generic"
    ssh_user: str = "cloud-user"


@dataclass
class VirshResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class OutsideLabError(PermissionError):
    """Raised when an operation targets a VM outside the lab namespace."""


class VirshClient:
    def __init__(self, uri: str = None, storage_path: str = None, templates_dir: str = None):
        self.uri = uri or settings.virsh_uri
        self.storage_path = Path(storage_path or settings.vm_storage_path)
        self.templates_dir = Path(templates_dir or settings.templates_dir)
        self._os_variants: Optional[set] = None

    def is_lab_vm(self, name: str) -> bool:
        """The configured prefix is a reserved namespace, not user authentication."""
        return (bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name))
                and len(name) <= 63 and name.startswith(settings.vm_name_prefix))

    def require_lab_vm(self, name: str):
        if not self.is_lab_vm(name):
            raise OutsideLabError("VM is outside the lab namespace")

    @staticmethod
    def _slug(value: str) -> str:
        """Lowercase and reduce to letters, digits and single hyphens."""
        text = (value or "").strip().lower()
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    @classmethod
    def _name_token(cls, value: str) -> str:
        """Slug for a template or image name, without its image extension."""
        if not value:
            return ""
        token = Path(value).name.lower()
        for suffix in IMAGE_SUFFIXES:
            if token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        return cls._slug(token)

    def provision_name(self, name: str, template: str = None) -> str:
        """Build the lab VM name: <prefix><template>-<name>.

        User input is normalised rather than rejected: letters are lowercased,
        and any run of unsupported characters becomes a single hyphen. So
        "My VM!", "my_vm" and "My-VM" all become "my-vm".
        """
        raw = (name or "").strip()
        slug = self._slug(raw)
        # An already qualified name is kept as-is (for example labs-web1).
        if raw and raw.lower().startswith(settings.vm_name_prefix) and self.is_lab_vm(slug):
            return slug
        token = self._name_token(template) if template else ""
        base = f"{token}-{slug}" if token else slug
        candidate = settings.vm_name_prefix + base
        if not self.is_lab_vm(candidate) or len(candidate) > 63:
            raise ValueError(
                "Enter a name with at least one letter or digit, "
                "up to 63 characters including the prefix and template"
            )
        return candidate

    @staticmethod
    def _from_completed(result: subprocess.CompletedProcess) -> "VirshResult":
        return VirshResult(
            success=result.returncode == 0,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )

    def _run_raw(self, cmd: list[str], timeout: int = 120) -> VirshResult:
        """Run a raw command (virt-install, qemu-img, cloud-localds)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return self._from_completed(result)
        except subprocess.TimeoutExpired:
            return VirshResult(success=False, stderr="Command timed out", returncode=-1)
        except Exception as e:
            return VirshResult(success=False, stderr=str(e), returncode=-1)

    def _run(self, args: list[str], timeout: int = 60) -> VirshResult:
        """A virsh command. Only enumeration or lab-VM targets are allowed."""
        if args and args[0] != "list" and len(args) > 1:
            self.require_lab_vm(args[1])
        return self._run_raw(["virsh", "-c", self.uri] + args, timeout=timeout)

    # ---------- VM Listing & Status ----------

    def list_vms(self) -> list[VMInfo]:
        """List all lab VMs with state, addresses and allocated resources."""
        result = self._run(["list", "--all", "--name"])
        if not result.success:
            return []

        vms = []
        for name in result.stdout.splitlines():
            name = name.strip()
            if not self.is_lab_vm(name):
                continue
            vms.append(self._build_vm_info(name))

        return vms

    def get_vm(self, name: str) -> Optional[VMInfo]:
        """Inspect a single lab VM without enumerating the whole fleet."""
        if not self.is_lab_vm(name) or not self.vm_exists(name):
            return None
        return self._build_vm_info(name)

    def _build_vm_info(self, name: str) -> VMInfo:
        state = self._get_vm_state(name)
        ips = self.get_vm_addresses(name)
        ip = next((a for a in ips if not _is_tailscale_ip(a)), None)
        ts_ip = next((a for a in ips if _is_tailscale_ip(a)), None)
        snapshots = self.list_snapshots(name)
        meta = self._get_vm_meta(name)
        user = meta.get("user") or settings.default_ssh_user
        spec = self.get_vm_spec(name)
        return VMInfo(name=name, state=state, ip=ip, tailscale_ip=ts_ip,
                      user=user, tailscale_enabled=meta.get("tailscale") == "1",
                      snapshots=snapshots, **spec)

    def get_vm_spec(self, name: str) -> dict:
        """Allocated vCPUs / memory (from dominfo) and disk (from qcow2)."""
        spec = {"vcpus": 0, "memory_mb": 0, "disk_gb": 0}

        info = self._run(["dominfo", name])
        if info.success:
            for line in info.stdout.splitlines():
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                try:
                    if key == "cpu(s)":
                        spec["vcpus"] = int(value)
                    elif key == "max memory":
                        spec["memory_mb"] = int(value.split()[0]) // 1024
                except (ValueError, IndexError):
                    pass

        disk_path = self.storage_path / f"{name}.qcow2"
        if disk_path.is_file():
            spec["disk_gb"] = self._virtual_size_gb(disk_path)

        return spec

    def _get_vm_state(self, name: str) -> str:
        result = self._run(["domstate", name])
        if result.success:
            return result.stdout
        return "unknown"

    def get_vm_addresses(self, name: str) -> list[str]:
        """All non-loopback IPv4 addresses reported by the guest agent."""
        result = self._run(["domifaddr", name, "--source", "agent"])
        if not result.success:
            return []

        # Name       MAC address          Protocol     Address
        # eth0       52:54:00:xx:xx:xx    ipv4         192.168.122.45/24
        # tailscale0                     ipv4         100.101.102.103/32
        addrs: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if "ipv4" not in parts:
                continue
            for part in parts:
                if "/" in part and part[0].isdigit():
                    addr = part.split("/")[0]
                    if not addr.startswith("127.") and addr not in addrs:
                        addrs.append(addr)
        return addrs

    def get_vm_ip(self, name: str) -> Optional[str]:
        """Primary (non-Tailscale) VM IP via the qemu guest agent."""
        addrs = self.get_vm_addresses(name)
        return next((a for a in addrs if not _is_tailscale_ip(a)), None)

    def get_vm_memory_stats(self, name: str) -> Optional[dict]:
        """Guest memory from the virtio balloon, in KB.

        The balloon driver reports values gathered inside the guest, so this is
        a guest-side view that works even where the agent's exec/file commands
        are disabled by policy (RHEL and CentOS blacklist them). Keys include
        ``actual`` (assigned), ``available`` (guest available) and ``unused``.
        """
        result = self._run(["dommemstat", name])
        if not result.success:
            return None
        stats: dict[str, int] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
                stats[parts[0]] = int(parts[1])
        return stats or None

    def get_tailscale_ip(self, name: str) -> Optional[str]:
        """Tailscale IP (100.64.0.0/10) as reported by the guest agent.

        No SSH needed: qemu-guest-agent reports every interface, including
        tailscale0 once `tailscale up` has run.
        """
        addrs = self.get_vm_addresses(name)
        return next((a for a in addrs if _is_tailscale_ip(a)), None)

    def agent_command(self, name: str, execute: str,
                      arguments: dict | None = None,
                      timeout: int = 15) -> Optional[dict]:
        """Call a QEMU guest agent command and return its ``return`` payload.

        Returns None when the agent is unavailable, the command is not
        supported, or the reply is malformed. This is the low-level primitive
        used by the metric collector and the guest-exec helpers.
        """
        payload = {"execute": execute}
        if arguments is not None:
            payload["arguments"] = arguments
        result = self._run(["qemu-agent-command", name, json.dumps(payload)],
                           timeout=timeout)
        if not result.success:
            return None
        try:
            return json.loads(result.stdout).get("return")
        except (ValueError, AttributeError):
            return None

    def _guest_exec_capture(self, name: str, script: str,
                            timeout: int = 8) -> Optional[dict]:
        """Run a shell command in the guest and return its captured output.

        Returns ``{"exitcode": int, "out": str, "err": str}`` or None if the
        agent is unavailable or the command does not finish in time. Guest
        output is base64 encoded by the agent.
        """
        start = {
            "path": "/bin/sh",
            "arg": ["-c", script],
            "capture-output": True,
        }
        begun = self.agent_command(name, "guest-exec", start, timeout=15)
        if not begun or "pid" not in begun:
            return None
        status_args = {"pid": begun["pid"]}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.agent_command(name, "guest-exec-status", status_args, timeout=15)
            if info is None:
                return None
            if info.get("exited"):
                def decode(key: str) -> str:
                    raw = info.get(key)
                    if not raw:
                        return ""
                    try:
                        return base64.b64decode(raw).decode("utf-8", "replace")
                    except (ValueError, TypeError):
                        return ""
                return {
                    "exitcode": info.get("exitcode", 0),
                    "out": decode("out-data"),
                    "err": decode("err-data"),
                }
            time.sleep(0.2)
        return None

    def _guest_exec(self, name: str, script: str, timeout: int = 8) -> bool:
        """Best-effort shell command in the guest; True once it exits."""
        return self._guest_exec_capture(name, script, timeout) is not None

    def guest_exec_capture(self, name: str, script: str,
                           timeout: int = 8) -> Optional[dict]:
        """Public form of the guest exec capture, safe to expose over RPC."""
        return self._guest_exec_capture(name, script, timeout)

    def logout_tailscale(self, name: str) -> bool:
        """Best-effort removal of a VM from the tailnet before it is deleted.

        ``tailscale logout`` deletes the node from the tailnet, so a deleted VM
        does not linger as an offline machine. It needs the guest to be running
        with a responsive agent; failures are ignored and never block deletion.
        """
        if not settings.tailscale_logout_on_delete:
            return False
        if self._get_vm_meta(name).get("tailscale") != "1":
            return False
        if self._get_vm_state(name) != "running":
            return False
        # The CLI lands in /usr/bin (package) or /usr/local/bin (static build).
        return self._guest_exec(
            name,
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
            "command -v tailscale >/dev/null 2>&1 && tailscale logout",
        )

    def get_vm_user(self, name: str) -> Optional[str]:
        """Guest login user, read back from the domain description.

        Provisioning records ``labforge:user=<name>`` in the libvirt domain
        description so per-VM credentials survive restarts without a side file.
        """
        return self._get_vm_meta(name).get("user")

    def _get_vm_meta(self, name: str) -> dict:
        """Parse ``labforge:<key>=<value>`` pairs from the domain description."""
        result = self._run(["desc", name])
        if not result.success:
            return {}
        return dict(re.findall(r"labforge:([a-z_]+)=(\S+)", result.stdout))

    def vm_exists(self, name: str) -> bool:
        result = self._run(["domstate", name])
        return result.success

    def ping(self) -> bool:
        """Cheap readiness probe: can we talk to libvirt?"""
        return self._run(["list", "--all", "--name"]).success

    # ---------- VM Lifecycle ----------

    def start_vm(self, name: str) -> VirshResult:
        return self._run(["start", name])

    def stop_vm(self, name: str) -> VirshResult:
        return self._run(["shutdown", name])

    def force_stop_vm(self, name: str) -> VirshResult:
        return self._run(["destroy", name])

    def reboot_vm(self, name: str) -> VirshResult:
        return self._run(["reboot", name])

    def delete_vm(self, name: str) -> VirshResult:
        """Destroy, delete snapshots, undefine, and remove disks.

        libvirt refuses to undefine a domain that still has snapshots, so
        they are deleted first - lab VMs are ephemeral and Delete means all
        of it.
        """
        self.require_lab_vm(name)

        # Ask the guest to remove itself from the tailnet first, while it is
        # still running. Best-effort: a stopped/unreachable guest (or a VM
        # without Tailscale) must never block deletion.
        try:
            self.logout_tailscale(name)
        except Exception:
            pass

        # Destroy if running
        self._run(["destroy", name])

        # Snapshots block undefine; remove them first
        for snapshot in self.list_snapshots(name):
            self._run(["snapshot-delete", name, snapshot])

        # Undefine (incl. any nvram) and remove storage
        result = self._run(["undefine", name, "--nvram", "--remove-all-storage"])
        if not result.success:
            # Fallback: manual cleanup, and report the fallback's outcome.
            fallback = self._run(["undefine", name])
            self._cleanup_disk_files(name)
            return fallback

        return result

    def _cleanup_disk_files(self, name: str):
        self.require_lab_vm(name)
        for suffix in (".qcow2", ".iso"):
            (self.storage_path / f"{name}{suffix}").unlink(missing_ok=True)
        # The seed ISO has a different name and must be removed too.
        (self.storage_path / f"seed-{name}.iso").unlink(missing_ok=True)

    # ---------- Provisioning ----------

    def _find_iso_tool(self) -> Optional[str]:
        """A tool that can build a seed ISO with extra files (authkey)."""
        for tool in ("genisoimage", "xorriso", "mkisofs"):
            if shutil.which(tool):
                return tool
        return None

    def _tailscale_key_file(self) -> Optional[str]:
        """Path to the Tailscale auth key file, if configured and present.

        The path comes from ``TAILSCALE_AUTH_KEY_FILE``. This only checks that
        the file exists; the contents are never read by the application. The
        file is byte-copied onto the guest seed ISO and consumed in the guest.
        """
        configured = settings.tailscale_auth_key_file
        if not configured:
            return None
        path = Path(configured)
        return str(path) if path.is_file() else None

    def tailscale_available(self) -> bool:
        """True when a key file is configured and a seed ISO tool is present."""
        return bool(self._tailscale_key_file() and self._find_iso_tool())

    def _seed_iso_command(self, build_dir: Path, seed_iso: Path) -> list[str]:
        """Command that builds the cloud-init seed ISO.

        Prefer genisoimage/xorriso so an ``authkey`` file can be included
        alongside user-data; fall back to cloud-localds.
        """
        files = [f for f in ("user-data", "meta-data", "network-config", "authkey")
                 if (build_dir / f).is_file()]
        paths = [str(build_dir / f) for f in files]
        tool = self._find_iso_tool()
        if tool == "genisoimage":
            return ["genisoimage", "-quiet", "-output", str(seed_iso),
                    "-volid", "cidata", "-joliet", "-rock"] + paths
        if tool in ("xorriso", "mkisofs"):
            return [tool, "-as", "mkisofs", "-quiet", "-output", str(seed_iso),
                    "-volid", "cidata", "-joliet", "-rock"] + paths
        return ["cloud-localds", "--network-config", str(build_dir / "network-config"),
                str(seed_iso), str(build_dir / "user-data"), str(build_dir / "meta-data")]

    def create_vm(self, *args, **kwargs) -> VirshResult:
        """Serialise provisioning so budget checks + disk creation are atomic."""
        with _create_lock:
            return self._create_vm_unlocked(*args, **kwargs)

    def _create_vm_unlocked(
        self,
        name: str,
        template_name: str = None,
        memory_mb: int = None,
        vcpus: int = None,
        disk_gb: int = None,
        enable_tailscale: bool = False,
        vm_user: str = None,
        vm_password: str = None,
        image: str = None,
        os_variant: str = None,
    ) -> VirshResult:
        """Create a new lab VM; never overwrite an existing domain or disk.

        The base disk comes either from a named template (``template_name``) or
        from a qcow2 uploaded to the import directory (``image``).
        """
        token = template_name or image
        name = self.provision_name(name, token)
        self.require_lab_vm(name)
        if self.vm_exists(name):
            return VirshResult(False, stderr="VM already exists")

        if not image and not template_name:
            return VirshResult(False, stderr="Provide a template name or an image")

        image_info = None
        if image:
            try:
                image_path = self.resolve_image(image)
            except ValueError as exc:
                return VirshResult(False, stderr=str(exc))
            image_info = self.inspect_image(image_path)
            if not image_info:
                return VirshResult(False, stderr=f"Image '{image}' is not a valid qcow2 file")
            template = self._image_as_template(image_path, disk_gb=image_info["disk_gb"])
            template_dir = None  # never search a user-writable upload dir
        else:
            template = self.get_template(template_name)
            if not template:
                return VirshResult(
                    success=False,
                    stderr=f"Template '{template_name}' not found",
                )
            template_dir = Path(template.path).parent

        # Fail fast before doing any work if the name is already taken on disk.
        base_qcow2 = Path(template.path)
        vm_qcow2 = self.storage_path / f"{name}.qcow2"
        seed_iso = self.storage_path / f"seed-{name}.iso"
        if vm_qcow2.exists() or seed_iso.exists():
            return VirshResult(False, stderr="VM disk or seed already exists; refusing overwrite")
        if not self.storage_path.is_dir() or not os.access(self.storage_path, os.W_OK):
            return VirshResult(
                False,
                stderr=f"VM_STORAGE_PATH is not an existing writable directory: {self.storage_path}",
            )

        base_bytes = (image_info or {}).get("virtual_bytes") or self._virtual_size_bytes(base_qcow2)
        memory_mb = memory_mb or template.memory_mb
        vcpus = vcpus or template.vcpus
        disk_gb = disk_gb or template.disk_gb
        if base_bytes and disk_gb * (1024 ** 3) < base_bytes:
            return VirshResult(
                False,
                stderr=f"Requested disk {disk_gb} GB is smaller than the image "
                       f"({-(-base_bytes // (1024 ** 3))} GB). qcow2 images cannot be shrunk.",
            )
        os_variant = self._resolve_os_variant(os_variant or template.os_variant)
        guest_user = vm_user or template.ssh_user or settings.default_ssh_user
        guest_password = vm_password if vm_password is not None else settings.vm_password

        # Tailscale is enabled by default; the auth key is only ever copied
        # from a file onto the seed ISO, never read into the application.
        ts_key_file = self._tailscale_key_file() if enable_tailscale else None
        ts_enabled = bool(ts_key_file and self._find_iso_tool())
        # 0. Reject provisioning if the lab budget / host cannot provide the resources.
        resource_result = check_resources(memory_mb, vcpus, disk_gb, vms=self.list_vms())
        if not resource_result["ok"]:
            return VirshResult(
                success=False,
                stderr="Host resources insufficient: " + "; ".join(resource_result["reasons"]),
            )

        # Render before touching storage: missing keys/templates must fail safely.
        if not settings.ssh_public_keys_file:
            return VirshResult(False, stderr="Configure SSH_PUBLIC_KEYS_FILE before provisioning")
        try:
            ssh_keys = [line.strip() for line in Path(settings.ssh_public_keys_file).read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
            if not ssh_keys or any(not line.startswith(("ssh-", "ecdsa-", "sk-")) for line in ssh_keys):
                return VirshResult(False, stderr="SSH_PUBLIC_KEYS_FILE must contain public SSH keys")
            context = dict(vm_name=name,
                           vm_user=guest_user,
                           ssh_keys=ssh_keys,
                           vm_password=guest_password,
                           tailscale_enabled=ts_enabled)
            user_data = self._render_template("user-data.j2", search_dir=template_dir, **context)
            meta_data = self._render_template("meta-data.j2", search_dir=template_dir, **context)
            network_config = self._render_template("network-config.j2", search_dir=template_dir, **context)
        except (OSError, ValueError, TypeError, TemplateError) as exc:
            return VirshResult(False, stderr=str(exc))

        if not base_qcow2.exists():
            return VirshResult(
                success=False,
                stderr=f"Base image not found: {base_qcow2}",
            )

        # Exclusive creation prevents concurrent requests from overwriting a disk.
        # A failure mid-copy must not leave a partial disk behind, or the name
        # would be permanently blocked by the overwrite guard above.
        try:
            with base_qcow2.open("rb") as source, vm_qcow2.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        except OSError as exc:
            vm_qcow2.unlink(missing_ok=True)
            return VirshResult(False, stderr=f"Could not copy the base image: {exc}")

        # From here on, any failure must clean up the files we created so the
        # name stays usable for a retry.
        seed_iso.touch()
        keep = False
        try:
            resize_result = self._run_raw(
                ["qemu-img", "resize", str(vm_qcow2), f"{disk_gb}G"]
            )
            if not resize_result.success:
                return resize_result

            # 2. Create cloud-init seed (private dir, secrets not world-readable)
            cloud_init_dir = Path(tempfile.mkdtemp(prefix=f"labforge-{name}-"))
            try:
                (cloud_init_dir / "user-data").write_text(user_data)
                (cloud_init_dir / "meta-data").write_text(meta_data)
                (cloud_init_dir / "network-config").write_text(network_config)

                if ts_enabled and ts_key_file:
                    # Byte copy only. The key is never read, decoded or logged.
                    shutil.copyfile(ts_key_file, cloud_init_dir / "authkey")

                for f in cloud_init_dir.iterdir():
                    f.chmod(0o600)

                iso_result = self._run_raw(
                    self._seed_iso_command(cloud_init_dir, seed_iso)
                )
                if not iso_result.success:
                    return iso_result
            finally:
                shutil.rmtree(cloud_init_dir, ignore_errors=True)

            # Seed holds the guest password / Tailscale key. Keep it off-limits
            # to other users but readable by the QEMU process (group qemu on
            # libvirt hosts), which must attach it to the domain.
            try:
                seed_iso.chmod(0o640)
            except OSError:
                pass

            # 3. virt-install with serial console (CRITICAL for browser console)
            # Matches create-vm-instance.yaml but adds --serial pty --console pty,target_type=serial
            graphics_args: list[str] = ["--graphics", "none"]
            if settings.graphics_type in ("vnc", "spice"):
                # Graphical console for the browser "Screen" view.
                # listen=127.0.0.1 keeps VNC host-local (LabForge bridges it).
                graphics_args = [
                    "--graphics",
                    f"{settings.graphics_type},listen={settings.graphics_listen}",
                ]

            install_result = self._run_raw([
                "virt-install", "--connect", self.uri,
                "--name", name,
                "--description", f"labforge:user={guest_user} labforge:tailscale={1 if ts_enabled else 0}",
                "--memory", str(memory_mb),
                "--vcpus", str(vcpus),
                "--disk", f"path={vm_qcow2},format=qcow2",
                # The seed goes on virtio, not IDE: distro "cloud" images can be
                # virtio-only and never see an IDE CD-ROM, which would leave
                # cloud-init with no datasource (Debian genericcloud does this).
                "--disk", f"path={seed_iso},format=raw,device=disk,bus=virtio,readonly=on",
                "--network", f"network={settings.vm_network},model=virtio",
                "--os-variant", os_variant,
                "--channel", "unix,target_type=virtio,name=org.qemu.guest_agent.0",
                "--import",
                "--noautoconsole",
                "--serial", "pty",
                "--console", "pty,target_type=serial",
            ] + graphics_args)

            if not install_result.success:
                return install_result

            keep = True
        finally:
            if not keep:
                # Remove the half-built VM so the name can be retried. virt-install
                # may have defined the domain before failing, so undefine it too.
                try:
                    self._run(["undefine", name, "--nvram"])
                except (PermissionError, OSError):
                    pass
                self._cleanup_disk_files(name)

        # The address is discovered on the next poll rather than blocking here.
        return VirshResult(success=True, stdout=f"VM {name} created")

    def _render_template(self, template_name: str, search_dir: Path = None, **kwargs) -> str:
        """Render a cloud-init template.

        Looks first in *search_dir* (a per-template directory, e.g.
        ``cloud_init_templates/rhel-10``) and falls back to the shared
        ``default`` directory. Undefined variables raise instead of silently
        producing broken cloud-init.
        """
        from jinja2 import Environment, FileSystemLoader, StrictUndefined

        candidates = []
        if search_dir is not None:
            candidates.append(Path(search_dir) / template_name)
        candidates.append(self.templates_dir / "default" / template_name)

        for path in candidates:
            if path.is_file():
                env = Environment(
                    loader=FileSystemLoader(str(path.parent)),
                    keep_trailing_newline=True,
                    undefined=StrictUndefined,
                )
                return env.get_template(path.name).render(**kwargs)

        raise FileNotFoundError(
            f"Cloud-init template not found: {template_name}. Looked in "
            f"{', '.join(str(c.parent) for c in candidates)}."
        )

    # ---------- Templates ----------

    def get_template(self, name: str) -> Optional[TemplateInfo]:
        """Get template info by name."""
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
            return None
        template_dir = self.templates_dir / name
        if not template_dir.is_dir():
            return None

        # Accept common cloud-image extensions; all are qcow2/qcow formats.
        image_files = []
        for pattern in ("*.qcow2", "*.img", "*.qcow", "*.raw"):
            image_files.extend(sorted(template_dir.glob(pattern)))
        if not image_files:
            return None

        # Read metadata if exists
        meta_file = template_dir / "template.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except (OSError, ValueError):
                meta = {}

        return TemplateInfo(
            name=name,
            path=str(image_files[0].resolve()),
            description=meta.get("description", ""),
            memory_mb=meta.get("memory_mb", settings.default_memory_mb),
            vcpus=meta.get("vcpus", settings.default_vcpus),
            disk_gb=meta.get("disk_gb", settings.default_disk_gb),
            os_variant=meta.get("os_variant", settings.default_os_variant),
            ssh_user=meta.get("ssh_user", settings.default_ssh_user),
        )

    # ---------- Uploaded images ----------

    def _import_dir(self) -> Path:
        return settings.resolved_import_dir

    def _qemu_img_info(self, path: Path) -> Optional[dict]:
        """Read image metadata. -U allows reading while a VM holds the lock."""
        res = self._run_raw(["qemu-img", "info", "-U", "--output=json", str(path)])
        if not res.success:
            return None
        try:
            return json.loads(res.stdout)
        except (ValueError, TypeError):
            return None

    def _virtual_size_bytes(self, path: Path) -> int:
        data = self._qemu_img_info(path)
        if not data:
            return 0
        try:
            return int(data.get("virtual-size", 0))
        except (TypeError, ValueError):
            return 0

    def _virtual_size_gb(self, path: Path) -> int:
        """Virtual size in whole GB, rounded up (never below the real size)."""
        size = self._virtual_size_bytes(path)
        return -(-size // (1024 ** 3)) if size else 0

    def inspect_image(self, path: Path) -> Optional[dict]:
        """Return metadata for a qcow2 image, or None when it is not qcow2."""
        data = self._qemu_img_info(path)
        if not data or data.get("format") != "qcow2":
            return None
        try:
            virtual_bytes = int(data.get("virtual-size", 0))
        except (TypeError, ValueError):
            virtual_bytes = 0
        disk_gb = -(-virtual_bytes // (1024 ** 3)) if virtual_bytes else settings.default_disk_gb
        return {"format": "qcow2", "disk_gb": disk_gb, "virtual_bytes": virtual_bytes}

    def list_imported_images(self) -> list[dict]:
        """qcow2 files a user uploaded or an operator placed in the import dir."""
        directory = self._import_dir()
        if not directory.is_dir():
            return []
        images = []
        for path in sorted(directory.iterdir()):
            name = path.name
            if name.startswith(".") or name.startswith(settings.vm_name_prefix):
                continue  # never surface temp files or live VM disks
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                size_gb = round(path.stat().st_size / (1024 ** 3), 1)
            except OSError:
                size_gb = 0
            images.append({
                "name": name,
                "size_gb": size_gb,
                "disk_gb": self._virtual_size_gb(path) or settings.default_disk_gb,
            })
        return images

    def resolve_image(self, ref: str) -> Path:
        """Resolve an uploaded image name to a path inside the import dir."""
        if not ref:
            raise ValueError("No image name given")
        directory = self._import_dir().resolve()
        candidate = Path(ref)
        if candidate.name.startswith("."):
            raise ValueError("Invalid image name")
        if candidate.is_absolute():
            path = candidate
        else:
            if "/" in ref or "\\" in ref or ref in (".", ".."):
                raise ValueError("Invalid image name")
            path = directory / ref
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            raise ValueError(f"Image '{ref}' not found")
        if directory not in resolved.parents:
            raise ValueError("Image is outside the import directory")
        if not resolved.is_file():
            raise ValueError("Image is not a file")
        if resolved.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError("Not a supported image file")
        if resolved.name.startswith(settings.vm_name_prefix):
            raise ValueError("Refusing to use a VM disk as a base image")
        return resolved

    def delete_imported_image(self, name: str) -> VirshResult:
        """Delete an uploaded image by name, resolved inside the import dir."""
        try:
            path = self.resolve_image(name)
        except ValueError as exc:
            return VirshResult(success=False, stderr=str(exc))
        try:
            path.unlink()
        except OSError as exc:
            return VirshResult(success=False, stderr=f"Could not delete image: {exc}")
        return VirshResult(success=True, stdout=f"Image '{name}' deleted")

    def _image_as_template(self, path: Path, disk_gb: int = None) -> TemplateInfo:
        name = path.stem
        low = name.lower()
        os_variant = settings.default_os_variant
        for token, variant in (("ubuntu", "ubuntu24.04"), ("debian", "debian12"),
                               ("fedora", "fedora44"), ("centos", "centos-stream10"),
                               ("rocky", "rocky9"), ("rhel", "rhel10")):
            if token in low:
                os_variant = variant
                break
        return TemplateInfo(
            name=name,
            path=str(path),
            description=f"Uploaded image: {path.name}",
            memory_mb=settings.default_memory_mb,
            vcpus=settings.default_vcpus,
            disk_gb=disk_gb or self._virtual_size_gb(path) or settings.default_disk_gb,
            os_variant=os_variant,
            ssh_user=settings.default_ssh_user,
        )

    def _supported_os_variants(self) -> set:
        """osinfo variants this host's virt-install understands."""
        if self._os_variants is None:
            variants: set = set()
            for args in (["virt-install", "--osinfo=list"],
                         ["virt-install", "--os-variant=list"]):
                res = self._run_raw(args)
                if res.success and res.stdout.strip():
                    for line in res.stdout.splitlines():
                        token = line.strip().split()[0].rstrip(",") if line.strip() else ""
                        if re.fullmatch(r"[a-z][a-z0-9.-]*", token or "") and "unknown" not in token:
                            variants.add(token)
                    if variants:
                        break
            self._os_variants = variants
        return self._os_variants

    @staticmethod
    def _version_key(variant: str) -> tuple:
        return tuple(int(n) for n in re.findall(r"\d+", variant or ""))

    def _resolve_os_variant(self, variant: str) -> str:
        """Map a desired variant onto one this host knows.

        Prefers the exact name, then the newest variant in the same family that
        is not newer than requested (for example fedora44 -> fedora43 when
        osinfo predates the release), then 'generic'. Provisioning therefore
        never fails on an unknown OS name.
        """
        supported = self._supported_os_variants()
        if not supported:
            return variant or "generic"
        if variant and variant in supported:
            return variant
        family = re.match(r"[^0-9]*", variant or "").group(0)
        if not family:
            return "generic"
        candidates = [v for v in supported if v.startswith(family)]
        if not candidates:
            return "generic"
        want = self._version_key(variant)
        not_newer = [c for c in candidates if self._version_key(c) <= want]
        pool = not_newer or candidates
        return max(pool, key=self._version_key)

    def list_templates(self) -> list[TemplateInfo]:
        """List all available templates (subdirs with qcow2)."""
        templates = []
        if not self.templates_dir.exists():
            return templates

        for item in self.templates_dir.iterdir():
            if item.is_dir() and item.name != "default":
                tpl = self.get_template(item.name)
                if tpl:
                    templates.append(tpl)
        return templates

    # ---------- Snapshots ----------

    def list_snapshots(self, vm_name: str) -> list[str]:
        result = self._run(["snapshot-list", vm_name, "--name"])
        if not result.success:
            return []
        return [s.strip() for s in result.stdout.splitlines() if s.strip()]

    def create_snapshot(self, vm_name: str, snapshot_name: str) -> VirshResult:
        return self._run(["snapshot-create-as", vm_name, snapshot_name, "--atomic"])

    def revert_snapshot(self, vm_name: str, snapshot_name: str) -> VirshResult:
        return self._run(["snapshot-revert", vm_name, snapshot_name])

    def delete_snapshot(self, vm_name: str, snapshot_name: str) -> VirshResult:
        return self._run(["snapshot-delete", vm_name, snapshot_name])

    # ---------- Console ----------

    def get_console_command(self, vm_name: str) -> list[str]:
        """Return the virsh console command for PTY spawn."""
        self.require_lab_vm(vm_name)
        return ["virsh", "-c", self.uri, "console", vm_name]

    def get_display(self, vm_name: str) -> Optional[tuple[str, int]]:
        """Read the actual VNC TCP port from live XML, not a display number."""
        from xml.etree import ElementTree
        result = self._run(["dumpxml", vm_name])
        if not result.success:
            return None
        try:
            graphics = ElementTree.fromstring(result.stdout).find("./devices/graphics[@type='vnc']")
            if graphics is None:
                return None
            listener = graphics.find("listen")
            host = (listener.get("address") if listener is not None else None) or graphics.get("listen")
            port = int(graphics.get("port", "-1"))
            if not host or not 1 <= port <= 65535:
                return None
            return host, port
        except (ElementTree.ParseError, ValueError):
            return None


# Singleton
_virsh_client: Optional[VirshClient] = None


def get_virsh_client() -> VirshClient:
    global _virsh_client
    if _virsh_client is None:
        _virsh_client = VirshClient()
    return _virsh_client