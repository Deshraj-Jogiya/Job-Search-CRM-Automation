#!/usr/bin/env bash
# One-time bootstrap for a fresh Oracle Cloud Always Free Oracle Linux 9
# instance (VM.Standard.A1.Flex, aarch64). Run this AS THE opc USER (the
# default account Oracle's own Linux image creates) over SSH -- it uses
# sudo internally where needed. Idempotent-ish: safe to re-run if a step
# fails partway, but it's meant for a fresh box.
#
# Usage: bash setup_oracle_vm.sh
set -euo pipefail

APP_DIR="/opt/career-pilot"
SERVICE_USER="career-pilot"
REPO_URL="git@github.com:Deshraj-Jogiya/Job-Search-CRM-Automation.git"

echo "== 1/6: system packages =="
# Oracle Linux 9's default python3 is 3.9 -- too old to reliably match
# requirements-lock.txt (pinned from a Python 3.13 dev environment).
# python3.11 is available directly from Oracle's own ol9_appstream repo,
# no EPEL needed.
sudo dnf install -y python3.11 python3.11-pip git

echo "== 2/6: dedicated service user (no login shell, no home dir surprises) =="
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    sudo useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "== 3/6: clone the repo (requires a deploy key already added to GitHub -- see README note below) =="
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$APP_DIR"
else
    echo "Repo already present at $APP_DIR -- pulling latest instead."
    sudo -u "$SERVICE_USER" git -C "$APP_DIR" pull
fi

echo "== 4/6: venv + dependencies (requirements-lock.txt -- the exact tested working set) =="
sudo -u "$SERVICE_USER" python3.11 -m venv "$APP_DIR/venv"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements-lock.txt"

echo "== 5/6: .env =="
if [ ! -f "$APP_DIR/.env" ]; then
    echo "No .env found at $APP_DIR/.env yet."
    echo "Copy your real .env there now (scp it over -- never through git), e.g. from your own machine:"
    echo "  scp -i ~/.ssh/oracle_career_pilot .env opc@<VM_PUBLIC_IP>:/tmp/env_upload"
    echo "  then on the VM: sudo mv /tmp/env_upload $APP_DIR/.env && sudo chown $SERVICE_USER:$SERVICE_USER $APP_DIR/.env && sudo chmod 600 $APP_DIR/.env"
    echo "Re-run this script after that's in place, or just continue -- the systemd service will fail to start until it exists."
fi

echo "== 6/6: systemd service =="
sudo cp "$APP_DIR/deploy/career-pilot.service" /etc/systemd/system/career-pilot.service
sudo systemctl daemon-reload
sudo systemctl enable career-pilot
if [ -f "$APP_DIR/.env" ]; then
    sudo systemctl restart career-pilot
    echo "Service started. Check status: sudo systemctl status career-pilot"
else
    echo "Service enabled but NOT started (.env missing). Start it once .env is in place:"
    echo "  sudo systemctl start career-pilot"
fi

echo ""
echo "Done. Next: point a reverse proxy (Caddy/nginx) at 127.0.0.1:8000 for TLS,"
echo "and open port 443 (and/or 8000 if testing without a proxy first) in both"
echo "the OCI Security List AND the OS firewall (iptables/ufw) -- see the"
echo "walkthrough notes for both of those, they're separate layers on Oracle."
