#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example and set the secrets first." >&2
  exit 1
fi

# Parse the Compose dotenv format safely; never execute .env as shell code.
source ./scripts/load_compose_env.sh
load_compose_env .env docker-compose.yml

: "${MQTT_CAMERA_EDGE_USERNAME:?Set MQTT_CAMERA_EDGE_USERNAME}"
: "${MQTT_CAMERA_EDGE_PASSWORD:?Set MQTT_CAMERA_EDGE_PASSWORD}"
: "${MQTT_SERVER_USERNAME:?Set MQTT_SERVER_USERNAME}"
: "${MQTT_SERVER_PASSWORD:?Set MQTT_SERVER_PASSWORD}"

docker compose run --rm --no-deps --user root \
  -e MQTT_CAMERA_EDGE_USERNAME -e MQTT_CAMERA_EDGE_PASSWORD \
  -e MQTT_SERVER_USERNAME -e MQTT_SERVER_PASSWORD \
  --entrypoint sh broker -c '
    set -eu
    PASSWORD_FILE=/mosquitto/data/password_file
    # Keep existing MQTT users; -c is only valid when the file does not exist.
    if [ -e "$PASSWORD_FILE" ]; then
      [ -f "$PASSWORD_FILE" ] || { echo "Mosquitto password path exists but is not a regular file: $PASSWORD_FILE" >&2; exit 1; }
      mosquitto_passwd -b "$PASSWORD_FILE" "$MQTT_CAMERA_EDGE_USERNAME" "$MQTT_CAMERA_EDGE_PASSWORD"
    else
      mosquitto_passwd -b -c "$PASSWORD_FILE" "$MQTT_CAMERA_EDGE_USERNAME" "$MQTT_CAMERA_EDGE_PASSWORD"
    fi
    mosquitto_passwd -b "$PASSWORD_FILE" "$MQTT_SERVER_USERNAME" "$MQTT_SERVER_PASSWORD"
    chown 1883:1883 "$PASSWORD_FILE"
    chmod 0640 "$PASSWORD_FILE"
  '
echo "Mosquitto camera-edge and django-server identities created."
