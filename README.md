# LabForge

KVM lab provisioner: cloud-init VMs, browser consoles, resource budget.

Control plane (UI/API) talks to **labforge-agent** on the KVM host. One
codebase, two roles. No controller RPM.

```
Browser → control (Helm / install-local / container)
              │  AGENT_URL + AGENT_TOKEN
              ▼
         labforge-agent.rpm  →  libvirt / disks / VNC
```

## Quick start (single host)

```bash
git clone <your-fork> labforge
cd labforge
./deploy/install-local.sh    # or: make install
```

Open http://127.0.0.1:8899. Add a template (`./scripts/fetch-templates.sh`),
then provision.

## Docs

| Doc | Contents |
|-----|----------|
| [Architecture](docs/architecture.md) | Control vs agent, modes |
| [Install](docs/install.md) | Local systemd install |
| [Deploy](docs/deploy.md) | Helm, Kustomize, packages, container |
| [Templates](docs/templates.md) | Images, uploads, naming |
| [Configuration](docs/configuration.md) | Environment variables |
| [Operations](docs/operations.md) | Credentials, Tailscale, capacity, metrics |
| [API](docs/api.md) | REST and WebSocket endpoints |
| [Security](docs/security.md) | Trust boundary |
| [Development](docs/development.md) | Tests, versioning, CI |
| [CentOS Stream 10 template](docs/template-setup.md) | Distro-specific image setup |

## License

MIT. Vendored: noVNC 1.7.0 (MPL-2.0) in `backend/app/static/novnc/`,
xterm.js (MIT) in `backend/app/static/xterm/`.
