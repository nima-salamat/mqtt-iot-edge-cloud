#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER="mqttiot"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run this script on Raspberry Pi OS / Debian Linux." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip rsync python3-lgpio
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir /var/lib/mqttiot --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if getent group gpio >/dev/null 2>&1; then
  sudo usermod -aG gpio "$SERVICE_USER"
fi

sudo mkdir -p /opt/mqtt-iot-edge-cloud
sudo rsync -a --delete --exclude='.venv' "$PROJECT_DIR/pi_device/" /opt/mqtt-iot-edge-cloud/pi_device/
sudo rsync -a "$PROJECT_DIR/deploy/mqtt-iot-pi.service" /etc/systemd/system/mqtt-iot-pi.service
sudo rsync -a "$PROJECT_DIR/pi_device/requirements.txt" /opt/mqtt-iot-edge-cloud/pi_device/requirements.txt
sudo chown -R "$SERVICE_USER:$SERVICE_USER" /opt/mqtt-iot-edge-cloud

sudo -u "$SERVICE_USER" python3 -m venv --system-site-packages /opt/mqtt-iot-edge-cloud/.venv
sudo -u "$SERVICE_USER" /opt/mqtt-iot-edge-cloud/.venv/bin/pip install --upgrade pip
sudo -u "$SERVICE_USER" /opt/mqtt-iot-edge-cloud/.venv/bin/pip install -r /opt/mqtt-iot-edge-cloud/pi_device/requirements.txt

if [[ ! -f /etc/mqtt-iot-pi.env ]]; then
  sudo install -m 600 "$PROJECT_DIR/pi_device/.env.pi.example" /etc/mqtt-iot-pi.env
  echo "Created /etc/mqtt-iot-pi.env. Edit MQTT_PASSWORD, LOCAL_WEB_PASSWORD and FLASK_SECRET_KEY before starting the service."
else
  echo "Keeping existing /etc/mqtt-iot-pi.env."
fi

sudo systemctl daemon-reload
sudo systemctl enable mqtt-iot-pi.service

echo "Installation prepared. After editing /etc/mqtt-iot-pi.env, run:"
echo "  sudo systemctl start mqtt-iot-pi"
echo "  sudo systemctl status mqtt-iot-pi"
echo "  hostname -I   # open http://PI_IP_ADDRESS:8080 from a device on your LAN"
