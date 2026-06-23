#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/MichaelGipson101/Netwatch.git"
INSTALL_DIR="${NETWATCH_INSTALL_DIR:-$HOME/netwatch}"

echo "[install] Netwatch installer"

if ! command -v apt >/dev/null 2>&1; then
    echo "[install] ERROR: this script requires a Debian/Ubuntu system with apt." >&2
    exit 1
fi

echo "[install] Installing OS and Python dependencies..."
sudo apt update
sudo apt install -y python3 nmap sqlite3 git python3-yaml python3-openpyxl

if [ -f "./monitor.py" ]; then
    echo "[install] Already inside a netwatch checkout, using $(pwd)"
    INSTALL_DIR="$(pwd)"
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "[install] $INSTALL_DIR already exists, using it as-is"
    else
        echo "[install] Cloning netwatch into $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    cd "$INSTALL_DIR"
fi

if [ ! -f "hosts.yaml" ]; then
    echo "[install] Seeding hosts.yaml from hosts.yaml.example..."
    cp hosts.yaml.example hosts.yaml
fi

RESTORE_PATH=""
if [ -t 0 ] || [ -e /dev/tty ]; then
    read -r -p "[install] Restore from a backup tarball? Enter path or press Enter to skip: " RESTORE_PATH < /dev/tty || true
else
    echo "[install] No terminal available for interactive prompts; skipping restore prompt."
    echo "[install] You can restore later with: python3 $INSTALL_DIR/monitor.py --restore <tarball>"
fi

if [ -n "$RESTORE_PATH" ]; then
    echo "[install] Restoring from $RESTORE_PATH..."
    python3 "$INSTALL_DIR/monitor.py" --restore "$RESTORE_PATH"
fi

CURRENT_USER="$(whoami)"
SERVICE_PATH="/etc/systemd/system/netwatch.service"

echo "[install] Writing systemd unit at $SERVICE_PATH..."
sudo tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=Netwatch homelab ping monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/monitor.py --no-tui --port 8080
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=netwatch

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now netwatch

LAN_IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "[install] Done. Netwatch is running."
echo "[install] Dashboard: http://${LAN_IP}:8080"
echo "[install] Open it in a browser to complete the first-run admin setup."
