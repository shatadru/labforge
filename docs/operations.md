# Operations

## Credentials

No built-in app login. Guest user and password are set at provision time (UI or
`vm_user` / `vm_password` in the API). Empty password → SSH keys only
(`ssh_pwauth: false`). The login user is stored in the libvirt domain
description for the copyable `ssh` command. A password, if set, lives only in
that VM's seed ISO (`0640`).

## Tailscale

On by default; disable per VM (`enable_tailscale: false`). Auth key is a file
path only — never entered in the UI:

```bash
install -m 600 /dev/null ~/.config/labforge/tailscale-authkey
printf '%s\n' 'tskey-auth-REPLACE_ME' > ~/.config/labforge/tailscale-authkey
# TAILSCALE_AUTH_KEY_FILE=/home/you/.config/labforge/tailscale-authkey
systemctl --user restart labforge.service
```

LabForge byte-copies the file onto the seed ISO (virtio disk). Use a
**reusable** key (preferably **ephemeral**) so deleted VMs leave the tailnet.
Missing key file → VM still creates, Tailscale skipped.

Debug: `ssh <user>@<ip> 'sudo tail -50 /var/log/labforge-tailscale.log'`

## Capacity

`RESOURCE_BUDGET_PERCENT` (default 50) caps lab share of host CPU/RAM/disk.
Dashboard and provision dialog enforce it; provisioning also fails if the host
is short on free memory or disk.

## Guest metrics

QEMU guest agent samples (no SSH): CPU, disk, load, memory (balloon fallback on
RHEL/CentOS). Background sampler interval: `METRICS_INTERVAL_SECONDS`.
`GET /api/v1/vms/{name}/usage` returns the latest cache.
