#!/usr/bin/env bash
# Runs on the EC2 instance after the workflow has synced the code.
# Installs dependencies, restarts the API and waits for /health.
set -euo pipefail

SERVICE=passportphotoapi
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "ERROR: $APP_DIR/.env is missing. Create it before deploying." >&2
  exit 1
fi

if [ ! -d venv ]; then
  # requirements.txt pins packages that need Python >= 3.12.
  PYTHON="$(command -v python3.13 || command -v python3.12 || command -v python3)"
  echo "Creating virtualenv with $PYTHON ($("$PYTHON" --version))..."
  "$PYTHON" -m venv venv
fi

echo "Installing dependencies..."
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt

# Install/refresh the systemd unit for whichever user and directory we deploy to.
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
RENDERED="$(sed -e "s|__USER__|$(id -un)|g" -e "s|__APP_DIR__|$APP_DIR|g" deploy/$SERVICE.service)"
if [ "$RENDERED" != "$(cat "$UNIT_FILE" 2>/dev/null)" ]; then
  echo "Installing $UNIT_FILE..."
  echo "$RENDERED" | sudo tee "$UNIT_FILE" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE"
fi

echo "Restarting $SERVICE..."
sudo systemctl restart "$SERVICE"

# First start after a fresh install can take several minutes
# (rembg/pymatting compile their numba kernels on first import).
echo "Waiting for /health..."
for _ in $(seq 1 120); do
  if curl -fs http://127.0.0.1:8000/health; then
    echo
    echo "Deploy OK"
    exit 0
  fi
  sleep 5
done

echo "ERROR: API did not become healthy within 10 minutes." >&2
sudo journalctl -u "$SERVICE" -n 80 --no-pager
exit 1
