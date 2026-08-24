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
REPO_URL="git@github.com:<your-github-username>/Job-Search-CRM-Automation.git"
# ^ update this to your own fork before running

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

# Oracle Linux 9 runs SELinux Enforcing -- anything that passed through
# /tmp (git clone's own temp objects, pip's build/cache dirs) keeps
# /tmp's user_tmp_t label, which breaks execution later with confusing,
# SELinux-less-looking errors (systemd reports plain "Permission
# denied" or "status=203/EXEC", never mentioning SELinux). Fixing the
# label now, once, is simpler than debugging it after the fact.
sudo restorecon -R "$APP_DIR"

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
sudo restorecon -v /etc/systemd/system/career-pilot.service
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
echo "Done -- but this only starts the app itself. See deploy/README.md for"
echo "everything else a real deployment needs: opening the firewall (two"
echo "separate layers on Oracle -- the OCI Security List AND firewalld),"
echo "Caddy for real HTTPS (works even without an owned domain, via"
echo "sslip.io), the Supabase IPv4-pooler gotcha if DATABASE_URL points at"
echo "Postgres, and the Xvfb/noVNC setup interactive autofill needs since"
echo "this VM has no real display."
