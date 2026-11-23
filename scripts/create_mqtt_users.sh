#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
  echo "Missing .env. Copy .env.example to .env and fill its secrets first." >&2
  exit 1
fi
set -a
. ./.env
set +a

docker compose run --rm --no-deps --entrypoint sh broker -c '
  set -eu
  mosquitto_passwd -b -c /mosquitto/data/password_file "$MQTT_DEVICE_USERNAME" "$MQTT_DEVICE_PASSWORD"
  mosquitto_passwd -b    /mosquitto/data/password_file "$MQTT_SERVER_USERNAME" "$MQTT_SERVER_PASSWORD"
  chmod 0600 /mosquitto/data/password_file
'
echo "MQTT users created in the persistent Mosquitto volume."
