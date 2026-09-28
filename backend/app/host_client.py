"""Host client seam: local (in-process) or remote (a labforge agent).

The control plane never calls libvirt directly through this module. It asks a
``HostClient`` for host facts and operations. In ``HOST_MODE=local`` the client
is the existing ``VirshClient`` (single host and development). In
``HOST_MODE=remote`` it is :class:`RemoteHostClient`, which forwards to a
``labforge agent`` running next to libvirt.

The agent exposes a single RPC endpoint plus a few concrete ones (health, info,
image upload, console/VNC WebSockets). Method names are allow-listed on the
agent, so this module is the client half of that contract.
"""
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.virsh_client import (
    TemplateInfo,
    VirshClient,
    VirshResult,
    VMInfo,
    get_virsh_client,
)

logger = logging.getLogger("labforge.host")

# Every method the control plane may call on a host. Anything not listed here is
# refused by the agent, so this set is also the security boundary.
RPC_METHODS: frozenset[str] = frozenset({
    # reads
    "get_vm", "list_vms", "get_vm_user", "vm_exists", "ping",
    "list_templates", "get_template", "tailscale_available",
    "list_imported_images", "list_snapshots",
    # mutation
    "provision_name", "create_vm", "delete_vm",
    "start_vm", "stop_vm", "reboot_vm", "force_stop_vm",
    "create_snapshot", "revert_snapshot", "delete_snapshot",
    "delete_imported_image",
    # guest metrics
    "agent_command", "guest_exec_capture", "get_vm_memory_stats",
    "get_vm_usage", "all_vm_usage",
})

# Methods whose JSON result must be rebuilt into a dataclass on this side.
_VM_RESULTS = {"get_vm"}
_VM_LIST_RESULTS = {"list_vms"}
_TEMPLATE_RESULTS = {"get_template"}
_TEMPLATE_LIST_RESULTS = {"list_templates"}
_VIRSH_RESULTS = {
    "create_vm", "delete_vm", "start_vm", "stop_vm", "reboot_vm",
    "force_stop_vm", "create_snapshot", "revert_snapshot", "delete_snapshot",
    "delete_imported_image",
}


class HostError(RuntimeError):
    """Raised when the agent is unreachable or returns an error."""


def host_mode() -> str:
    """The effective host mode, forced local when running as the agent."""
    if settings.mode == "agent":
        return "local"
    return settings.host_mode


class RemoteHostClient:
    """Talks to a labforge agent over HTTP for management and guest metrics.

    Only the methods in :data:`RPC_METHODS` exist; anything else raises
    ``AttributeError`` so the seam stays explicit.
    """

    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: int = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout or settings.agent_timeout_seconds

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _call(self, method: str, *args, **kwargs):
        url = f"{self.base_url}/agent/v1/rpc/{method}"
        try:
            resp = httpx.post(url, json={"args": list(args), "kwargs": kwargs},
                              headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise HostError(f"agent {self.base_url} unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise HostError("agent rejected the token")
        if resp.status_code >= 400:
            raise HostError(f"agent error {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        return self._decode(method, payload.get("result"))

    def _decode(self, method: str, result):
        if method in _VM_RESULTS:
            return VMInfo(**result) if result else None
        if method in _VM_LIST_RESULTS:
            return [VMInfo(**item) for item in result]
        if method in _TEMPLATE_RESULTS:
            return TemplateInfo(**result) if result else None
        if method in _TEMPLATE_LIST_RESULTS:
            return [TemplateInfo(**item) for item in result]
        if method in _VIRSH_RESULTS:
            return VirshResult(**result)
        if method in ("get_vm_usage",):
            from app.vm_metrics import GuestUsage
            return GuestUsage(**result) if result else None
        if method in ("all_vm_usage",):
            from app.vm_metrics import GuestUsage
            return {name: GuestUsage(**item) for name, item in (result or {}).items()}
        return result

    def upload_image(self, filename: str, stream, content_type: str = None) -> dict:
        """Forward an uploaded image to the agent's import directory."""
        url = f"{self.base_url}/agent/v1/images"
        try:
            resp = httpx.post(url, headers=self._headers(), timeout=self.timeout,
                              files={"file": (filename, stream, content_type or "application/octet-stream")})
        except httpx.HTTPError as exc:
            raise HostError(f"agent {self.base_url} unreachable: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise HostError(detail)
        return resp.json()

    def subscribe_ws_url(self, path: str) -> str:
        """WebSocket URL on the agent for a console/VNC path."""
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/agent/v1{path}"

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in RPC_METHODS:
            raise AttributeError(name)

        def call(*args, **kwargs):
            return self._call(name, *args, **kwargs)

        return call


def get_host_client():
    """Return the host client for the configured mode.

    ``local`` returns the shared ``VirshClient`` (it already satisfies the
    interface). ``remote`` returns a :class:`RemoteHostClient`.
    """
    if host_mode() == "remote":
        if not settings.agent_url:
            raise HostError("HOST_MODE=remote requires AGENT_URL")
        _warn_if_insecure(settings.agent_url)
        return RemoteHostClient(settings.agent_url, settings.agent_token)
    return get_virsh_client()


def _warn_if_insecure(url: str) -> None:
    """Warn when a non-loopback agent URL would send the token in clear text."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if (parsed.hostname or "") in ("127.0.0.1", "localhost", "::1"):
        return
    logger.warning(
        "AGENT_URL %s is not https; the agent token will be sent in clear text. "
        "Use https on any untrusted network.", url,
    )
