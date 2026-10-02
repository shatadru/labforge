# Architecture

One codebase, two roles:

| Mode | Role | Default port |
|------|------|--------------|
| `MODE=control` | UI, API, WebSocket proxy | 8899 (systemd) / 8000 (container) |
| `MODE=agent` | Owns libvirt on a KVM host | 8443 |

```
Browser
  → Control plane (MODE=control)
       REST /api/v1/*, HTMX UI, WS /ws/console|vnc
       Host client: local VirshClient or RemoteHostClient
  → labforge-agent (MODE=agent) on the KVM host
       RPC allow-list + AGENT_TOKEN
       libvirt, disks, VNC on 127.0.0.1
```

## How the control plane reaches libvirt

| Setting | Behavior |
|---------|----------|
| `HOST_MODE=local` | In-process `VirshClient` (single host) |
| `HOST_MODE=remote` | One `AGENT_URL` + `AGENT_TOKEN` (one hypervisor) |
| `LOCAL_AGENT=true` | Control starts an in-process agent on loopback |

Kubernetes/Helm deploys **control only** (`HOST_MODE=remote`). Libvirt always
stays on the `labforge-agent` package. `AGENT_URL` is one host, not a fleet.

## Agent auth

- Bearer `AGENT_TOKEN` required unless `AGENT_ALLOW_ANONYMOUS=true` (dev only).
- `GET /agent/v1/health` stays open for probes.
- Only methods in `host_client.RPC_METHODS` are callable.

For a remote control plane, set `AGENT_BIND` on the agent to a non-loopback
address the control plane can reach.
