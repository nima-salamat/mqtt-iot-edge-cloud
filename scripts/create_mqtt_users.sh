#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
  echo "Missing .env. Copy .env.example and set the secrets first." >&2
  exit 1
fi
set -a
. ./.env
set +a

: "${MQTT_CAMERA_EDGE_USERNAME:?Set MQTT_CAMERA_EDGE_USERNAME}"
: "${MQTT_CAMERA_EDGE_PASSWORD:?Set MQTT_CAMERA_EDGE_PASSWORD}"
: "${MQTT_SERVER_USERNAME:?Set MQTT_SERVER_USERNAME}"
: "${MQTT_SERVER_PASSWORD:?Set MQTT_SERVER_PASSWORD}"

docker compose run --rm --no-deps --user root \
  -e MQTT_CAMERA_EDGE_USERNAME -e MQTT_CAMERA_EDGE_PASSWORD \
  -e MQTT_SERVER_USERNAME -e MQTT_SERVER_PASSWORD \
  --entrypoint sh broker -c '
    set -eu
    mosquitto_passwd -b -c /mosquitto/data/password_file "$MQTT_CAMERA_EDGE_USERNAME" "$MQTT_CAMERA_EDGE_PASSWORD"
    mosquitto_passwd -b    /mosquitto/data/password_file "$MQTT_SERVER_USERNAME" "$MQTT_SERVER_PASSWORD"
    chown 1883:1883 /mosquitto/data/password_file
    chmod 0640 /mosquitto/data/password_file
  '
echo "Mosquitto camera-edge and django-server identities created."
