# Operations

Day-two tasks after install.

```mermaid
flowchart TD
  P[Provision] --> Creds{Password set?}
  Creds -->|No| SSH[SSH keys only]
  Creds -->|Yes| Seed[Password in seed ISO 0640]
  P --> TS{Tailscale key file?}
  TS -->|Yes| Join[Guest joins tailnet]
  TS -->|No| Skip[Tailscale skipped]
  P --> Cap[Budget and free host capacity checked]
```

## Credentials

No built-in app login. Guest user and password are set at provision time (UI or
`vm_user` / `vm_password` in the API).

| Choice | Result |
|--------|--------|
| Empty password | SSH keys only (`ssh_pwauth: false`) |
| Password set | Written only into that VM's seed ISO (`0640`) |
| Login user | Stored in the libvirt domain description for the copyable `ssh` line |

## Tailscale

On by default. Disable per VM with `enable_tailscale: false`.

Auth key is a **file path only** (never typed in the UI):

```bash
install -m 600 /dev/null ~/.config/labforge/tailscale-authkey
printf '%s\n' 'tskey-auth-REPLACE_ME' > ~/.config/labforge/tailscale-authkey
# TAILSCALE_AUTH_KEY_FILE=/home/you/.config/labforge/tailscale-authkey
systemctl --user restart labforge.service
```

LabForge byte-copies the file onto the seed ISO (virtio disk). Use a
**reusable** key, preferably **ephemeral**, so deleted VMs leave the tailnet.
If the file is missing, the VM still creates and Tailscale is skipped.

Debug:

```bash
ssh <user>@<ip> 'sudo tail -50 /var/log/labforge-tailscale.log'
```

## Capacity

`RESOURCE_BUDGET_PERCENT` (default 50) caps the lab share of host CPU, RAM, and
disk. The dashboard and provision dialog enforce it. Provisioning also fails if
the host is short on free memory or disk.

## Guest metrics

Sampled through the QEMU guest agent (no SSH): CPU, disk, load, memory
(balloon fallback on RHEL/CentOS). Interval: `METRICS_INTERVAL_SECONDS`.

Latest sample: `GET /api/v1/vms/{name}/usage`.
