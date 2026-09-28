from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default template location, relative to the package (works both in the repo and
# in the container where the app is installed under /app).
_DEFAULT_TEMPLATES_DIR = str(Path(__file__).resolve().parent.parent / "cloud_init_templates")

# Values that mean "the operator forgot to set a real token". Rejecting these at
# startup keeps a known-public bearer token out of a running agent.
_PLACEHOLDER_TOKENS = {
    "changeme",
    "change_me",
    "change-me",
    "changeme-auth-token-replace",
    "changeme-local-dev-token-replace",
    "changeme-token-replace",
    "secret",
    "password",
    "token",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
    )

    # App
    app_name: str = "LabForge"
    log_level: str = "info"

    # Deployment mode. "control" serves the UI/API; "agent" runs on a KVM host
    # and owns libvirt. One codebase, two entrypoints.
    mode: str = Field(default="control", alias="MODE", pattern=r"^(control|agent)$")

    # How the control plane reaches the host that owns libvirt:
    #   local  - in-process VirshClient (single host, dev)
    #   remote - talk to a labforge agent over HTTP/WebSocket
    host_mode: str = Field(default="local", alias="HOST_MODE", pattern=r"^(local|remote)$")
    agent_url: str | None = Field(default=None, alias="AGENT_URL")
    # Bearer token the control plane presents to the agent. Never logged.
    agent_token: str | None = Field(default=None, alias="AGENT_TOKEN")
    agent_timeout_seconds: int = Field(default=120, ge=5, le=3600,
                                       alias="AGENT_TIMEOUT_SECONDS")

    # Agent listen settings (MODE=agent).
    agent_bind: str = Field(default="127.0.0.1", alias="AGENT_BIND")
    agent_port: int = Field(default=8443, ge=1, le=65535, alias="AGENT_PORT")
    agent_name: str = Field(default="", alias="AGENT_NAME")
    agent_advertise_url: str | None = Field(default=None, alias="AGENT_ADVERTISE_URL")

    # Run the agent in-process alongside the control plane (single-system dev/test).
    # When true, MODE=control generates a token, starts the agent on
    # AGENT_BIND:AGENT_PORT, and points the control plane at it automatically.
    local_agent: bool = Field(default=False, alias="LOCAL_AGENT")

    # Explicit opt-in for an unauthenticated agent. Intended only for local
    # development; never enable this on a networked host.
    agent_allow_anonymous: bool = Field(default=False, alias="AGENT_ALLOW_ANONYMOUS")

    @field_validator("agent_token")
    @classmethod
    def _validate_agent_token(cls, value: str | None) -> str | None:
        """Normalise blank tokens to None and reject placeholders."""
        if value is None:
            return None
        token = value.strip()
        if not token:
            return None
        if token.lower() in _PLACEHOLDER_TOKENS:
            raise ValueError(
                "AGENT_TOKEN is still a placeholder value; set a strong random "
                "token (for example: openssl rand -hex 32)"
            )
        return token

    # Libvirt
    virsh_uri: str = Field(default="qemu:///system", alias="VIRSH_URI")
    vm_storage_path: str = Field(default="/var/lib/libvirt/images", alias="VM_STORAGE_PATH")
    templates_dir: str = Field(default=_DEFAULT_TEMPLATES_DIR, alias="TEMPLATES_DIR")

    # Where qcow2 files uploaded from the browser are stored. Defaults to
    # <VM_STORAGE_PATH>/imports when unset.
    import_dir: str | None = Field(default=None, alias="IMPORT_DIR")
    # Maximum accepted upload size in gigabytes.
    max_upload_gb: int = Field(default=64, ge=1, le=1024, alias="MAX_UPLOAD_GB")

    # Reserved namespace for lab VMs; never use an empty prefix.
    vm_name_prefix: str = Field(default="labs-", min_length=2, max_length=32,
                                pattern=r"^[a-z][a-z0-9-]*-$")
    ssh_public_keys_file: str | None = None
    default_ssh_user: str = "cloud-user"
    default_os_variant: str = "generic"
    vm_network: str = "default"

    # Console
    console_enabled: bool = True
    console_max_sessions: int = Field(default=5, ge=1, le=64)

    # Graphical console (browser VNC via noVNC)
    graphics_type: str = Field(default="vnc", alias="GRAPHICS_TYPE",
                               pattern=r"^(vnc|spice|none)$")
    graphics_listen: str = Field(default="127.0.0.1", alias="GRAPHICS_LISTEN")

    # Guest credentials are injected per VM at creation time and are never
    # stored or defaulted here. VM_PASSWORD is only an optional server-wide
    # fallback for unattended provisioning.
    vm_password: str | None = Field(default=None, alias="VM_PASSWORD")

    # Path to a file holding the Tailscale auth key, supplied via the
    # environment. No default. The key is ONLY ever byte-copied onto the guest
    # seed ISO; the application never reads, logs or returns its contents.
    # When unset, Tailscale is skipped and the VM is still created.
    tailscale_auth_key_file: str | None = Field(default=None, alias="TAILSCALE_AUTH_KEY_FILE")

    # When a VM is deleted, ask a running guest to run `tailscale logout` so
    # the node is removed from the tailnet instead of lingering as an offline
    # machine. Best-effort and only possible while the guest is running.
    tailscale_logout_on_delete: bool = Field(default=True, alias="TAILSCALE_LOGOUT_ON_DELETE")

    # Guest metrics, read through the QEMU guest agent.
    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")
    metrics_interval_seconds: int = Field(default=5, ge=2, le=300,
                                          alias="METRICS_INTERVAL_SECONDS")

    # Resource overhead / lab budget
    resource_memory_overhead_mb: int = 512
    resource_disk_overhead_gb: int = 2
    # Share of host capacity LabForge may allocate to lab VMs (CPU/RAM/disk).
    resource_budget_percent: int = Field(default=50, ge=1, le=100,
                                         alias="RESOURCE_BUDGET_PERCENT")

    # Cloud image defaults
    default_memory_mb: int = 1536
    default_vcpus: int = 2
    default_disk_gb: int = 30

    @property
    def resolved_import_dir(self) -> Path:
        """Directory for uploaded qcow2 files."""
        return Path(self.import_dir) if self.import_dir else Path(self.vm_storage_path) / "imports"

    @property
    def agent_bind_is_loopback(self) -> bool:
        """True when the agent only listens on the local machine."""
        return self.agent_bind in ("127.0.0.1", "localhost", "::1", "")

    def api_base(self) -> str:
        """Base URL the control plane uses to reach this agent."""
        return f"http://{self.agent_bind}:{self.agent_port}"


APP_VERSION = "0.1.0"
settings = Settings()
