#!/usr/bin/env bash
# One-command local install for LabForge on a single KVM host.
#
# Creates the Python venv, prepares directories and config under
# ~/.config/labforge, installs the user systemd unit, and starts it. With
# LOCAL_AGENT=true the control plane and the agent run in one process.
#
# Usage:  deploy/install-local.sh [repo-root]
set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKEND="$REPO_ROOT/backend"
CONFIG_DIR="$HOME/.config/labforge"
UNIT_DIR="$HOME/.config/systemd/user"
VENV="$BACKEND/.venv"

echo "LabForge local install"
echo "  repo:    $REPO_ROOT"
echo "  config:  $CONFIG_DIR"
echo

if ! command -v virsh >/dev/null 2>&1; then
    echo "warning: virsh not found; install libvirt-client/qemu-kvm before provisioning" >&2
fi

# 1. Python venv and dependencies.
if [ ! -x "$VENV/bin/uvicorn" ]; then
    echo "Creating venv at $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$BACKEND/requirements.txt"
else
    echo "Reusing venv at $VENV"
fi

# 2. Config directory and env file (absolute paths substituted for __HOME__).
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/labforge.env" ]; then
    sed "s|__HOME__|$HOME|g" "$REPO_ROOT/deploy/labforge.env.example" \
        > "$CONFIG_DIR/labforge.env"
    echo "Wrote $CONFIG_DIR/labforge.env"
else
    echo "Keeping existing $CONFIG_DIR/labforge.env"
fi

# 3. Storage directory for lab VMs.
STORAGE="$HOME/labforge/vms"
mkdir -p "$STORAGE"
echo "VM storage: $STORAGE"

# 4. User systemd unit.
mkdir -p "$UNIT_DIR"
cp "$REPO_ROOT/deploy/labforge.service" "$UNIT_DIR/labforge.service"
systemctl --user daemon-reload
systemctl --user enable --now labforge.service

echo
echo "LabForge is running at http://localhost:8899"
echo "Logs:   journalctl --user -u labforge -f"
echo "Config: $CONFIG_DIR/labforge.env"
