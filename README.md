# LabForge

A lightweight, Kubernetes native lab provisioner for KVM virtual machines.
Provision VMs from cloud images with cloud-init, get SSH and a browser console,
and keep everything inside a resource budget you control.

## Features

- **Dashboard** with live host activity: CPU, memory and disk with sparklines.
- **Guest usage** per VM, read inside the running guest through the QEMU guest agent.
- **Lab capacity card**: what is allocated, what is left, and how many more VMs fit.
- **Templates**: drop a cloud image plus a small `template.json` into a directory.
- **Per VM credentials**: the login user and password are supplied at creation time.
- **Optional Tailscale** on every VM, enabled by default, with the tailnet IP shown in the UI.
- **Serial console** in the browser (xterm.js over a WebSocket to `virsh console`).
- **Graphical console** in the browser (vendored noVNC over a WebSocket to QEMU VNC).
- **Snapshots**: create, revert and delete from the VM page.
- **Single container** or a plain systemd service. No build step for the UI.
- **GitOps ready**: Kustomize base and overlays, plus a Helm chart.

## How it works

```
Browser
  |
  v
FastAPI (one process)
  |-- REST API      /api/v1/*
  |-- HTMX UI       Jinja2 templates
  |-- WebSocket     /ws/console/{vm}  -> virsh console (PTY)
  |-- WebSocket     /ws/vnc/{vm}      -> QEMU VNC (TCP bridge)
  `-- virsh client  (subprocess)
  |
  v
libvirt on the host:  /var/run/libvirt, /var/lib/libvirt/images
```

## Requirements

- A Linux host with libvirt, QEMU and the `virsh` client.
- `genisoimage` or `xorriso` (used to build the cloud-init seed ISO).
- `qemu-img` and `cloud-localds` (from `qemu-utils` and `cloud-image-utils`).
- Python 3.11 or newer.

## Run locally, step by step

1. Clone the repository and enter the backend directory.

   ```bash
   git clone <your-fork> labforge
   cd labforge/backend
   ```

2. Run the one-command installer, or set it up by hand.

   ```bash
   deploy/install-local.sh          # or: make install
   ```

   The installer creates the venv, prepares `~/.config/labforge/labforge.env`
   with absolute paths, installs the systemd user unit (`LOCAL_AGENT=true` runs
   the control plane and the agent together), and starts it. Open
   http://localhost:8899.

   To do it by hand instead:

   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   sudo install -d -o "$USER" -g qemu -m 2775 /var/lib/libvirt/images/labforge
   install -m 600 /dev/null ~/.ssh/labforge_authorized_keys
   cat ~/.ssh/id_ed25519.pub >> ~/.ssh/labforge_authorized_keys
   mkdir -p ~/.config/labforge
   cp ../deploy/labforge.env.example ~/.config/labforge/labforge.env
   $EDITOR ~/.config/labforge/labforge.env   # set the paths to absolute
   set -a; . ~/.config/labforge/labforge.env; set +a
   uvicorn app.main:app --host 0.0.0.0 --port 8899
   ```

3. Add at least one template (see the next section). Open http://localhost:8899.

## Templates

A template is a directory under `TEMPLATES_DIR` that contains a cloud image and
an optional `template.json`:

```
$TEMPLATES_DIR/
  default/                     # shared cloud-init templates (Jinja2)
    user-data.j2
    meta-data.j2
    network-config.j2
  fedora-44/
    Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
    template.json
```

`template.json` fields (all optional):

```json
{
  "description": "Fedora 44 Generic Cloud",
  "memory_mb": 1536,
  "vcpus": 2,
  "disk_gb": 30,
  "os_variant": "fedora44",
  "ssh_user": "cloud-user"
}
```

Recognised image extensions: `.qcow2`, `.img`, `.qcow`, `.raw`.
A per-template `user-data.j2` overrides the shared one in `default/`.

Download the bundled Ubuntu and Debian images and verify their checksums:

```bash
./scripts/fetch-templates.sh
```

## Provision from your own qcow2

You can upload a qcow2 straight from the browser and provision from it, with the
same cloud-init treatment as a template (login user, password, SSH keys and
optional Tailscale).

- On the Templates page click **Provision from qcow2**.
- Add the file in any of these ways:
  - use the browser's file control and pick it,
  - drag the file from your file manager onto the upload box,
  - or copy it into the import directory on the host and pick it from the list.
- It uploads automatically and is selected for you, then set the name, size and
  credentials as usual.
- Uploads are streamed to the import directory and validated: only a real qcow2
  is accepted. The virtual size of the image is used as the default disk size,
  and the image is never shrunk. Accepted names: `.qcow2`, `.qcow`, `.img`, `.raw`.

The import directory defaults to `<VM_STORAGE_PATH>/imports` and can be changed
with `IMPORT_DIR`. Uploads are capped by `MAX_UPLOAD_GB` (default 64).

The qcow2 is used as a base image. LabForge copies it to the VM disk and resizes
it, so the uploaded file is never modified and can back many VMs.

## VM naming

Every VM is named `<prefix><template>-<name>`, for example provisioning `web1`
from `fedora-44` creates `labs-fedora-44-web1`. Provisioning from an uploaded
image uses the image name without its extension, so `ubuntu-24.04.qcow2` yields
`labs-ubuntu-24-04-web1` (dots become hyphens because a libvirt name may not
contain them). The provision dialog shows the resulting name live as you type.

The name is normalised, not rejected: type it in any case and with spaces,
underscores or punctuation, and LabForge lowercases it and collapses separators
into single hyphens. For example `My VM!` becomes `my-vm`. Names are capped at
63 characters and checked against the reserved prefix, so LabForge never lists
or touches VMs outside its namespace.

## Configuration

All runtime configuration is supplied through the environment. The systemd unit
loads `~/.config/labforge/labforge.env`. Every value has a safe default, so only
override what you need.

| Variable | Default | Description |
|---|---|---|
| `VIRSH_URI` | `qemu:///system` | libvirt connection URI |
| `VM_NAME_PREFIX` | `labs-` | reserved namespace for lab VMs |
| `TEMPLATES_DIR` | package relative | directory of cloud images and `template.json` |
| `IMPORT_DIR` | `<VM_STORAGE_PATH>/imports` | where browser qcow2 uploads are stored |
| `MAX_UPLOAD_GB` | `64` | maximum accepted upload size |
| `VM_STORAGE_PATH` | `/var/lib/libvirt/images` | writable directory for disks and seed ISOs |
| `SSH_PUBLIC_KEYS_FILE` | none | public keys injected into new VMs (required to provision) |
| `DEFAULT_MEMORY_MB` | `1536` | default memory for a new VM |
| `DEFAULT_VCPUS` | `2` | default vCPUs for a new VM |
| `DEFAULT_DISK_GB` | `30` | default disk for a new VM |
| `DEFAULT_SSH_USER` | `cloud-user` | fallback login user |
| `RESOURCE_BUDGET_PERCENT` | `50` | share of host CPU, memory and disk lab VMs may use |
| `CONSOLE_ENABLED` | `true` | enable the browser consoles |
| `CONSOLE_MAX_SESSIONS` | `5` | maximum concurrent console sessions per VM |
| `GRAPHICS_TYPE` | `vnc` | graphics for new VMs: `vnc`, `spice` or `none` |
| `GRAPHICS_LISTEN` | `127.0.0.1` | address QEMU graphics listen on |
| `VM_PASSWORD` | none | optional server wide fallback password |
| `TAILSCALE_AUTH_KEY_FILE` | none | path to a file holding a Tailscale auth key |
| `TAILSCALE_LOGOUT_ON_DELETE` | `true` | ask a running guest to leave the tailnet before deletion |
| `METRICS_ENABLED` | `true` | collect guest usage through the QEMU guest agent |
| `METRICS_INTERVAL_SECONDS` | `5` | how often running VMs are sampled |
| `LOG_LEVEL` | `info` | log level |

## Credentials

There is no built in username or password. Both are supplied when a VM is
created:

- **UI**: the provision dialog has a Login user field and a Password field.
- **API**: send `vm_user` and `vm_password` in the provision request.
- Leave the password empty for SSH key only access. Cloud-init then sets
  `ssh_pwauth: false` and locks the password.

The chosen user is recorded in the libvirt domain description, so the copyable
`ssh <user>@<ip>` command is correct for each VM.

## Tailscale

Tailscale is enabled by default and can be turned off per VM with the Join
Tailscale tailnet checkbox or `enable_tailscale: false` in the API.

The auth key is never entered in the UI and never stored in the application.
Place it in a file and point LabForge at it:

```bash
install -m 600 /dev/null ~/.config/labforge/tailscale-auth
printf '%s\n' 'tskey-auth-REPLACE_ME' > ~/.config/labforge/tailscale-auth
# in ~/.config/labforge/labforge.env add:
# TAILSCALE_AUTH_KEY_FILE=/home/you/.config/labforge/tailscale-auth
systemctl --user restart labforge.service
```

How it is handled:

- LabForge only checks that the file exists. It never reads, logs or returns the key.
- The file is byte-copied onto the VM seed ISO, which is created `0640` inside a
  private temporary directory.
- The seed is attached to the guest as a virtio disk, not an IDE CD-ROM. Some
  cloud images, such as Debian's `genericcloud` build, are virtio-only and never
  see an IDE CD-ROM, which would leave cloud-init without a datasource and the
  guest without a key. The guest finds the seed by scanning its block devices
  for the `authkey` file, so it works whether the seed appears as a disk or a
  CD-ROM.
- On first boot the guest reads the key from the seed and installs Tailscale,
  trying three routes in order: the official installer, then the RPM repository
  written directly (the installer needs the `dnf` config-manager plugin, which
  an unsubscribed RHEL image lacks), then the static binary. It then runs
  `tailscale up`, retrying a few times if the network or daemon is not ready.
  Nothing sensitive is written to user-data or to any log.
- The tailnet IP (`100.64.0.0/10`) is read back through the QEMU guest agent and
  shown on the dashboard and the VM page.
- Deleting a VM asks the running guest to run `tailscale logout` first (via the
  same QEMU guest agent, best-effort, no admin API key). Note that Tailscale
  only removes a node from the tailnet immediately on logout if the node is
  **ephemeral**. With a normal auth key the node stays in the tailnet as an
  offline machine. See the note below on ephemeral keys. A stopped or
  unreachable guest is still deleted; only its node would remain.

**Use a reusable auth key.** The same key file is used for every VM. A
single-use (non-reusable) key is consumed by the first VM, and every VM after
that installs Tailscale but stays logged out. Create the key with
**Reusable** enabled in the Tailscale admin console.

**Prefer a reusable *ephemeral* key.** Mark the key **Ephemeral** as well as
Reusable. Ephemeral nodes are built for short-lived machines: Tailscale removes
them from the tailnet automatically when they disconnect, and immediately when
the guest runs `tailscale logout`. Combined with the logout-on-delete behavior
above, a deleted VM then disappears from the tailnet on its own. A non-ephemeral
key leaves each deleted VM behind as an offline machine until key expiry (180
days by default), which is exactly the stale-node buildup an ephemeral key
avoids. Ephemeral nodes cannot be used as subnet routers or exit nodes, which is
fine for lab VMs.

If a VM never shows a tailnet IP, inspect the guest log:

```bash
ssh <user>@<libvirt-ip> 'sudo tail -50 /var/log/labforge-tailscale.log'
```

If no key file is configured, the VM is still created and Tailscale is skipped.

## Capacity and budget

LabForge never assumes it owns the whole host. `RESOURCE_BUDGET_PERCENT`
(default `50`) is the share of the host CPU, memory and disk that lab VMs may
use. The dashboard shows the budget as a live card with used versus total per
dimension, and how many more VMs of a given size still fit. The provision dialog
re-checks capacity as you edit the size and disables Provision with the exact
reason when a VM will not fit.

Provisioning is also refused when the host itself is short on free memory or
disk, even if the budget would allow it.

## Guest metrics

Host usage and guest usage are different things. The dashboard host card
measures the hypervisor; the per VM figures describe what the machine sees from
the inside, so a VM that shows 90% memory is its own guest, not the host.

Metrics are read through the QEMU guest agent, with no SSH:

- CPU: `guest-get-cpustats` deltas between samples, the same technique the host
  card uses on `/proc/stat`.
- Disk: `guest-get-fsinfo` (the root filesystem).
- Load: `guest-get-load`; vCPUs: `guest-get-vcpus`.
- Memory: the agent does not expose used memory, so `/proc/meminfo` is read once
  through `guest-exec`. RHEL and CentOS disable the agent's exec and file
  commands by policy, so there it falls back to the virtio balloon
  (`virsh dommemstat`), whose `available` value is also gathered inside the
  guest.

A background sampler (`METRICS_INTERVAL_SECONDS`, default 5) polls every running
VM and caches the result, so page requests never block on a slow or missing
agent. The dashboard VM card shows a compact CPU/MEM/DSK strip; the VM page
shows a full "VM Resources" card mirroring the host card, labelled as guest
usage. Stopped VMs and guests without a running agent simply show that metrics
are unavailable. Read the latest sample with `GET /api/v1/vms/{name}/usage`.

## Testing

```bash
cd backend
pip install -r requirements-dev.txt

pytest                       # full suite
pytest --cov=app             # with coverage
tox                          # isolated environment, used by CI
make test-cov                # convenience wrapper
```

Templates are rendered with Jinja2 strict undefined in tests, so a missing
variable fails the build instead of rendering an empty page.

## Packaging

Pick whichever fits your environment. All of them read the same environment
variables, so the application behaves identically.

### Container

The control plane listens on 8000 in the container. Mount the templates
directory and the libvirt socket (with host networking so the VNC bridge can
reach QEMU on 127.0.0.1).

```bash
docker build -t labforge:0.1.0 backend
docker run --rm --network host \
  --env-file ~/.config/labforge/labforge.env \
  -v /var/run/libvirt:/var/run/libvirt \
  -v /var/lib/libvirt/images:/var/lib/libvirt/images \
  -v /var/lib/libvirt/labforge/templates:/app/cloud_init_templates:ro \
  labforge:0.1.0
```

The control plane and the agent can also run as separate services. See
"Deployment modes" below.

### Kubernetes with Kustomize

```bash
kubectl apply -k k8s/overlays/dev
```

Create the guest keys Secret first:

```bash
kubectl -n labforge create secret generic labforge-ssh-keys \
  --from-file=authorized_keys=$HOME/.ssh/id_ed25519.pub
```

### Kubernetes with Helm

```bash
helm install labforge charts/labforge \
  --namespace labforge --create-namespace \
  --set image.repository=ghcr.io/OWNER/labforge \
  --set image.tag=0.1.0 \
  --set storage.hostPath=/var/lib/libvirt/images/labforge \
  --set templates.hostPath=/var/lib/libvirt/labforge/templates \
  --set sshKeys.secretName=labforge-ssh-keys
```

See `charts/labforge/values.yaml` for every option.

### Debian and RPM packages

The package installs the **agent** (the host-side service) under
`/usr/lib/labforge`, its configuration under `/etc/labforge/labforge-agent.env`,
and the `labforge-agent.service` systemd unit.

```bash
# Build both with nfpm (https://nfpm.goreleaser.com)
make package          # produces labforge-agent_<version>_amd64.deb and .rpm
sudo dpkg -i dist/labforge-agent_*.deb
sudo rpm -i dist/labforge-agent-*.rpm
# then set AGENT_TOKEN in /etc/labforge/labforge-agent.env
sudo systemctl enable --now labforge-agent
```

### systemd user service

The simplest single-host option. `deploy/install-local.sh` creates the venv,
writes `~/.config/labforge/labforge.env` with the right absolute paths, installs
the user unit, and starts it:

```bash
deploy/install-local.sh          # or: make install
```

With `LOCAL_AGENT=true` (the default in the example) the control plane and the
agent run in one process.

## Deployment modes

One codebase, two roles, chosen with `MODE`:

| Mode | Role | Default port |
|------|------|--------------|
| `MODE=control` (default) | UI, API, scheduler | 8899 (systemd) / 8000 (container) |
| `MODE=agent` | owns libvirt on a KVM host | 8443 |

The control plane reaches the host through `HOST_MODE`:

- `HOST_MODE=local` - in-process `VirshClient` (single host).
- `HOST_MODE=remote` - talk to an agent over HTTP/WebSockets. Set `AGENT_URL`
  and a strong `AGENT_TOKEN` (for example `openssl rand -hex 32`). Use `https`
  and `wss` on an untrusted network; the token is sent as a Bearer header.
- `LOCAL_AGENT=true` - control starts the agent in-process on loopback and
  points itself at it, so a single systemd unit runs both roles.

The agent refuses requests without a token unless `AGENT_ALLOW_ANONYMOUS=true`,
which is for local development only. Only the RPC methods in
`host_client.RPC_METHODS` are callable.

## API

Interactive documentation is served at `/api/docs` and `/api/redoc`.

```
GET    /api/v1/vms                              list VMs
GET    /api/v1/vms/{name}                       one VM
GET    /api/v1/vms/{name}/usage                 guest usage (QEMU agent)
POST   /api/v1/vms                              provision
POST   /api/v1/vms/{name}/start|stop|reboot     lifecycle
POST   /api/v1/vms/{name}/reset                 delete (reset to a clean state)
DELETE /api/v1/vms/{name}                       delete
GET    /api/v1/templates                        list templates
GET    /api/v1/images                           list uploaded qcow2 images
POST   /api/v1/images                           upload a qcow2 (multipart, field "file")
DELETE /api/v1/images/{name}                    delete an uploaded image
GET    /api/v1/vms/{name}/snapshots             list snapshots
POST   /api/v1/vms/{name}/snapshots             create snapshot
POST   /api/v1/vms/{name}/snapshots/{s}/revert  revert snapshot
DELETE /api/v1/vms/{name}/snapshots/{s}         delete snapshot
GET    /api/v1/host/usage                       live host metrics
GET    /api/v1/host/capacity                    lab budget and per template fit
GET    /api/v1/host/check                       preflight one VM size
GET    /api/health                              liveness
GET    /api/ready                               readiness
WS     /ws/console/{name}                       serial console
WS     /ws/vnc/{name}                           graphical console
```

Provision body:

Provide a template or an uploaded image, exactly one:

```json
{
  "name": "demo",
  "template": "fedora-44",
  "memory_mb": 1536,
  "vcpus": 2,
  "disk_gb": 30,
  "vm_user": "lab",
  "vm_password": "choose-a-password",
  "enable_tailscale": true
}
```

Or, after uploading a qcow2 with `POST /api/v1/images`:

```json
{ "name": "demo", "image": "ubuntu-24.04.qcow2", "enable_tailscale": true }
```

## Security

- There is no built in authentication. LabForge can start, delete and console
  into VMs, so expose it only to trusted users. Bind it to localhost or place it
  behind an authenticating reverse proxy.
- WebSockets reject cross origin connections, and the VNC bridge only dials
  loopback addresses.
- Guest credentials are supplied per VM and never stored. The seed ISO is
  `0640`, the temporary build directory is private and removed immediately.
- The VM name prefix is a namespace, not an authorization boundary.

## License

MIT.

Vendored third party components: noVNC 1.7.0 (MPL-2.0) under
`backend/app/static/novnc/`, and xterm.js (MIT) loaded from a CDN.
