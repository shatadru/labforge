# Configuration

Runtime config is environment variables. systemd loads
`~/.config/labforge/labforge.env` (control) or `/etc/labforge/labforge-agent.env`
(agent). Override only what you need.

| Variable | Default | Description |
|---|---|---|
| `MODE` | `control` | `control` or `agent` |
| `HOST_MODE` | `local` | `local` or `remote` |
| `LOCAL_AGENT` | `false` | In-process agent with control |
| `AGENT_URL` | none | Remote agent base URL |
| `AGENT_TOKEN` | none | Bearer token (required for agent unless anonymous) |
| `AGENT_BIND` | `127.0.0.1` | Agent listen address |
| `AGENT_PORT` | `8443` | Agent port |
| `AGENT_ALLOW_ANONYMOUS` | `false` | Dev-only unauthenticated agent |
| `API_KEY` | none | Optional lock on `/api/v1` |
| `VIRSH_URI` | `qemu:///system` | libvirt URI |
| `VM_NAME_PREFIX` | `labs-` | Lab VM namespace |
| `TEMPLATES_DIR` | package relative | Cloud images + `template.json` |
| `IMPORT_DIR` | `<VM_STORAGE_PATH>/imports` | Browser qcow2 uploads |
| `MAX_UPLOAD_GB` | `64` | Upload size cap |
| `VM_STORAGE_PATH` | `/var/lib/libvirt/images` | Disks and seed ISOs |
| `SSH_PUBLIC_KEYS_FILE` | none | Public keys for guests (required to provision) |
| `DEFAULT_MEMORY_MB` | `1536` | Default RAM |
| `DEFAULT_VCPUS` | `2` | Default vCPUs |
| `DEFAULT_DISK_GB` | `30` | Default disk |
| `DEFAULT_SSH_USER` | `cloud-user` | Fallback login user |
| `RESOURCE_BUDGET_PERCENT` | `50` | Host share for lab VMs |
| `CONSOLE_ENABLED` | `true` | Browser consoles |
| `CONSOLE_MAX_SESSIONS` | `5` | Consoles per VM |
| `GRAPHICS_TYPE` | `vnc` | `vnc`, `spice`, or `none` |
| `GRAPHICS_LISTEN` | `127.0.0.1` | Must be loopback |
| `VM_PASSWORD` | none | Optional server-wide fallback password |
| `TAILSCALE_AUTH_KEY_FILE` | none | Path to Tailscale auth key file |
| `TAILSCALE_LOGOUT_ON_DELETE` | `true` | `tailscale logout` before delete |
| `METRICS_ENABLED` | `true` | QEMU guest agent metrics |
| `METRICS_INTERVAL_SECONDS` | `5` | Sample interval |
| `LOG_LEVEL` | `info` | Log level |
| `APP_VERSION` | from `VERSION` file | UI label / cache bust |

Examples: `deploy/labforge.env.example`, `deploy/labforge-agent.env.example`.
