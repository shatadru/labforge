# Architecture

LabForge is two roles in one codebase.

| Mode | What it does | Default port |
|------|--------------|--------------|
| `MODE=control` | UI, API, WebSocket proxy | 8899 (systemd) / 8000 (container) |
| `MODE=agent` | Owns libvirt on a KVM host | 8443 |

```mermaid
flowchart TB
  subgraph Control["Control plane (MODE=control)"]
    UI[HTMX UI]
    API["REST /api/v1"]
    WS["WS /ws/console and /ws/vnc"]
    HC[Host client]
  end

  subgraph Agent["labforge-agent (MODE=agent)"]
    RPC["RPC allow-list + AGENT_TOKEN"]
    Virsh[virsh / virt-install]
  end

  Browser --> UI
  Browser --> API
  Browser --> WS
  UI --> HC
  API --> HC
  WS --> HC
  HC -->|"local VirshClient<br/>or RemoteHostClient"| RPC
  RPC --> Virsh
  Virsh --> Disks["Disks + VNC on 127.0.0.1"]
```

## How control reaches libvirt

```mermaid
flowchart LR
  subgraph Local["HOST_MODE=local"]
    C1[Control] --> V1[VirshClient in-process]
  end

  subgraph Remote["HOST_MODE=remote"]
    C2[Control] -->|"AGENT_URL + token"| A2[labforge-agent]
    A2 --> V2[libvirt on that host]
  end

  subgraph Combo["LOCAL_AGENT=true"]
    C3[Control] --> Loop[In-process agent on loopback]
  end
```

| Setting | Behavior |
|---------|----------|
| `HOST_MODE=local` | In-process `VirshClient` (single host) |
| `HOST_MODE=remote` | One `AGENT_URL` + `AGENT_TOKEN` (one hypervisor) |
| `LOCAL_AGENT=true` | Control starts an in-process agent on loopback |

Kubernetes and Helm deploy **control only** (`HOST_MODE=remote`). Libvirt always
stays behind the `labforge-agent` package. One `AGENT_URL` is one host, not a fleet.

## Agent auth

- Bearer `AGENT_TOKEN` required unless `AGENT_ALLOW_ANONYMOUS=true` (dev only).
- `GET /agent/v1/health` stays open for probes.
- Only methods in `host_client.RPC_METHODS` are callable.

For a remote control plane, set `AGENT_BIND` on the agent to an address the
control plane can reach (not `127.0.0.1`).
