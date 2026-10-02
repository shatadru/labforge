# Install (single host)

## Requirements

- Linux with libvirt, QEMU, `virsh`, `virt-install`
- `genisoimage` or `xorriso`, `qemu-img`, `cloud-localds`
- Python 3.11+

## One-command install

From the **repo root** (any checkout path):

```bash
git clone <your-fork> labforge
cd labforge
./deploy/install-local.sh    # or: make install
```

That script:

1. Creates `backend/.venv`
2. Writes `~/.config/labforge/labforge.env` (absolute paths for this checkout)
3. Creates `~/.ssh/labforge_authorized_keys` from your `id_*.pub` if missing
4. Installs the user systemd unit with `LOCAL_AGENT=true`
5. Binds the UI to `127.0.0.1:8899`

Open http://127.0.0.1:8899. Then add a [template](templates.md).

Enable linger and libvirt group access:

```bash
loginctl enable-linger "$USER"
# add yourself to the host libvirt group, then re-login
```

## Manual run

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
mkdir -p "$HOME/labforge/vms"
install -m 600 /dev/null ~/.ssh/labforge_authorized_keys
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/labforge_authorized_keys
mkdir -p ~/.config/labforge
sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$(cd .. && pwd)|g" \
  ../deploy/labforge.env.example > ~/.config/labforge/labforge.env
set -a; . ~/.config/labforge/labforge.env; set +a
uvicorn app.main:app --host 127.0.0.1 --port 8899
```

For multi-host or Kubernetes, see [Deploy](deploy.md).
