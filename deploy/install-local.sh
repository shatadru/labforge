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
KEYS_FILE="$HOME/.ssh/labforge_authorized_keys"

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

# 2. Config directory and env file (absolute paths from this checkout).
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/labforge.env" ]; then
    sed \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__REPO__|$REPO_ROOT|g" \
        "$REPO_ROOT/deploy/labforge.env.example" \
        > "$CONFIG_DIR/labforge.env"
    echo "Wrote $CONFIG_DIR/labforge.env"
else
    echo "Keeping existing $CONFIG_DIR/labforge.env"
fi

# 3. Guest SSH public keys (dedicated file, not the operator login keys).
if [ ! -f "$KEYS_FILE" ]; then
    install -m 600 /dev/null "$KEYS_FILE"
    if [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
        cat "$HOME/.ssh/id_ed25519.pub" >> "$KEYS_FILE"
    elif [ -f "$HOME/.ssh/id_rsa.pub" ]; then
        cat "$HOME/.ssh/id_rsa.pub" >> "$KEYS_FILE"
    else
        echo "warning: no ~/.ssh/id_*.pub found; add keys to $KEYS_FILE before provisioning" >&2
    fi
    echo "Wrote $KEYS_FILE"
fi

# 4. Storage directory for lab VMs.
STORAGE="$HOME/labforge/vms"
mkdir -p "$STORAGE"
echo "VM storage: $STORAGE"

# 5. User systemd unit with paths for this checkout.
mkdir -p "$UNIT_DIR"
sed \
    -e "s|__BACKEND__|$BACKEND|g" \
    -e "s|__VENV__|$VENV|g" \
    "$REPO_ROOT/deploy/labforge.service" \
    > "$UNIT_DIR/labforge.service"
systemctl --user daemon-reload
systemctl --user enable --now labforge.service

echo
echo "LabForge is running at http://127.0.0.1:8899"
echo "Logs:   journalctl --user -u labforge -f"
echo "Config: $CONFIG_DIR/labforge.env"
echo "Note:   enable linger if the service should survive logout:"
echo "        loginctl enable-linger \"$USER\""
echo "        and add yourself to the host libvirt group."
