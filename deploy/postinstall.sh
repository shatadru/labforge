#!/bin/sh
# Post-install for the LabForge agent package.
#
# Creates the service user and the Python venv, then enables the agent unit.
# The application code ships without a venv so the package stays small and the
# environment is built for the target Python and architecture.
set -e

APP_DIR=/usr/lib/labforge
VENV="$APP_DIR/venv"

# 1. Dedicated system user.
if ! id labforge >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/labforge --create-home \
        --shell /usr/sbin/nologin labforge >/dev/null 2>&1 || true
fi

# 2. Python virtualenv with the app dependencies.
if [ ! -x "$VENV/bin/uvicorn" ]; then
    python3 -m venv "$VENV" || true
    "$VENV/bin/pip" install --quiet --upgrade pip || true
    "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" || true
fi
chown -R labforge:labforge "$APP_DIR" 2>/dev/null || true

# 3. systemd.
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable labforge-agent.service >/dev/null 2>&1 || true
fi

echo "LabForge agent installed."
echo "  1. Set AGENT_TOKEN in /etc/labforge/labforge-agent.env (openssl rand -hex 32)"
echo "  2. Adjust VM_STORAGE_PATH, SSH_PUBLIC_KEYS_FILE and TAILSCALE_AUTH_KEY_FILE"
echo "  3. systemctl start labforge-agent"

exit 0
