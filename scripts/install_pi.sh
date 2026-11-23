#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER="mqttiot"
INSTALL_DIR="/opt/mqtt-iot-edge-cloud"
DATA_DIR="/var/lib/mqttiot"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run this installer on Raspberry Pi OS / Debian Linux." >&2
  exit 1
fi
if ! command -v dpkg >/dev/null 2>&1; then
  echo "This installer requires apt/dpkg (Raspberry Pi OS)." >&2
  exit 1
fi
ARCH="$(dpkg --print-architecture)"
if [[ "$ARCH" != "arm64" && "$ARCH" != "armhf" ]]; then
  echo "Expected Raspberry Pi ARM architecture, got $ARCH." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip python3-dev \
  python3-picamera2 python3-av python3-opencv python3-numpy \
  python3-libcamera python3-lgpio ffmpeg libzbar0 \
  libopenblas-dev libgomp1 rsync curl ca-certificates \
  cmake g++ git pybind11-dev protobuf-compiler libprotobuf-dev

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
for group in video render gpio; do
  if getent group "$group" >/dev/null 2>&1; then
    sudo usermod -aG "$group" "$SERVICE_USER"
  fi
done

sudo mkdir -p "$INSTALL_DIR/scripts" "$DATA_DIR/video" "$DATA_DIR/alarms" "$DATA_DIR/logs" "$DATA_DIR/tmp"
sudo rsync -a --delete --exclude='.venv' --exclude='models/*.bin' --exclude='models/*.param' "$PROJECT_DIR/pi_device/" "$INSTALL_DIR/pi_device/"
sudo rsync -a "$PROJECT_DIR/scripts/download_yolov5n_ncnn.py" "$INSTALL_DIR/scripts/download_yolov5n_ncnn.py"
sudo rsync -a "$PROJECT_DIR/deploy/mqtt-iot-pi.service" /etc/systemd/system/mqtt-iot-pi.service
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"

sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv --system-site-packages "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/pi_device/requirements.txt"

# Use the Raspberry Pi ARM wheel index first. Camera/OpenCV/AV remain OS packages
# so pip does not replace the libcamera-compatible system bindings.
if ! sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --no-deps \
  --extra-index-url https://www.piwheels.org/simple ncnn==1.0.20261020; then
  echo "No compatible NCNN wheel found; building NCNN Python bindings locally."
  if [[ ! -d "$INSTALL_DIR/ncnn-src/.git" ]]; then
    sudo -u "$SERVICE_USER" git clone --depth 1 https://github.com/Tencent/ncnn.git "$INSTALL_DIR/ncnn-src"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR/ncnn-src" submodule update --init --depth 1
  fi
  sudo -u "$SERVICE_USER" cmake -S "$INSTALL_DIR/ncnn-src" -B "$INSTALL_DIR/ncnn-src/build" \
    -DNCNN_PYTHON=ON -DNCNN_BUILD_TOOLS=OFF -DNCNN_BUILD_EXAMPLES=OFF \
    -DNCNN_BUILD_BENCHMARK=OFF -DNCNN_VULKAN=OFF
  sudo -u "$SERVICE_USER" cmake --build "$INSTALL_DIR/ncnn-src/build" --parallel 2
  sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install "$INSTALL_DIR/ncnn-src/python"
fi

if ! sudo -u "$SERVICE_USER" env PYTHONPATH="$INSTALL_DIR" \
  "$INSTALL_DIR/.venv/bin/python" -c "import picamera2, av, cv2, numpy, ncnn"; then
  echo "Picamera2 / PyAV / OpenCV / NumPy / NCNN imports failed." >&2
  echo "Check Raspberry Pi OS package availability and the Python environment." >&2
  exit 1
fi

sudo -u "$SERVICE_USER" env MODEL_DIR="$INSTALL_DIR/pi_device/models" \
  "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/download_yolov5n_ncnn.py"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"
sudo chmod 0644 "$INSTALL_DIR/pi_device/models/yolov5n.ncnn.param" "$INSTALL_DIR/pi_device/models/yolov5n.ncnn.bin"

# Load the actual NCNN weights and run one synthetic inference before enabling systemd.
sudo -u "$SERVICE_USER" env PYTHONPATH="$INSTALL_DIR" MODEL_DIR="$INSTALL_DIR/pi_device/models" \
  "$INSTALL_DIR/.venv/bin/python" -c \
  "import numpy as np; from pi_device.detector import YoloV5nDetector; detector=YoloV5nDetector(); print('YOLOv5n NCNN loaded; synthetic detections:', len(detector.detect(np.zeros((240,320,3),dtype=np.uint8))))"

if [[ ! -f /etc/mqtt-iot-pi.env ]]; then
  sudo install -m 600 "$PROJECT_DIR/pi_device/.env.pi.example" /etc/mqtt-iot-pi.env
  echo "Created /etc/mqtt-iot-pi.env."
  echo "Set MQTT_PASSWORD, LOCAL_WEB_PASSWORD and FLASK_SECRET_KEY before starting."
else
  echo "Keeping existing /etc/mqtt-iot-pi.env. Review it for the current camera settings."
fi

sudo systemctl daemon-reload
sudo systemctl enable mqtt-iot-pi.service

echo
echo "Installation prepared. Edit /etc/mqtt-iot-pi.env, then run:"
echo "  sudo systemctl start mqtt-iot-pi"
echo "  sudo systemctl status mqtt-iot-pi"
echo "  sudo journalctl -u mqtt-iot-pi -f"
echo "  hostname -I   # local panel: http://PI_IP_ADDRESS:8080"
