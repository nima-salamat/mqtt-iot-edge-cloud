#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${PI_INSTALL_DIR:-/opt/mqtt-iot-edge-cloud}"
SERVICE_NAME="${PI_SERVICE_NAME:-mqtt-iot-pi}"
SERVICE_USER="${PI_SERVICE_USER:-mqttiot}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run this updater on Raspberry Pi OS / Debian Linux." >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo ./scripts/update_pi.sh" >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/pi_device/app.py" || ! -f "$PROJECT_DIR/pi_device/bridge.py" ]]; then
  echo "The source checkout does not look like mqtt-iot-edge-cloud." >&2
  exit 1
fi
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  echo "The installed Python environment is missing: $INSTALL_DIR/.venv/bin/python" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Service account does not exist: $SERVICE_USER" >&2
  exit 1
fi

SOURCE_COMMIT="$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "Deploying source $SOURCE_COMMIT to $INSTALL_DIR"
echo "Only pi_device source files will be synchronized."
echo "The virtualenv, model weights, /etc/mqtt-iot-pi.env and /var/lib/mqttiot will be preserved."

systemctl stop "$SERVICE_NAME"
rsync -a --delete \
  --exclude='.venv' \
  --exclude='models/*.bin' \
  --exclude='models/*.param' \
  "$PROJECT_DIR/pi_device/" "$INSTALL_DIR/pi_device/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/pi_device"
runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/python" -m compileall -q "$INSTALL_DIR/pi_device"

systemctl start "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME"

echo
echo "Recent startup and MQTT diagnostics:"
journalctl -u "$SERVICE_NAME" -n 60 --no-pager
