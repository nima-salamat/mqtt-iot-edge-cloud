#!/usr/bin/env sh
set -eu

: "${STACK_NAME:=mqtt-iot}"
: "${MQTT_DEVICE_USERNAME:?Set MQTT_DEVICE_USERNAME in .env}"
: "${MQTT_DEVICE_PASSWORD:?Set MQTT_DEVICE_PASSWORD in .env}"
: "${MQTT_SERVER_USERNAME:?Set MQTT_SERVER_USERNAME in .env}"
: "${MQTT_SERVER_PASSWORD:?Set MQTT_SERVER_PASSWORD in .env}"

VOLUME_NAME="${STACK_NAME}_mosquitto_data"
docker volume create "$VOLUME_NAME" >/dev/null

docker run --rm --entrypoint sh \
  -e MQTT_DEVICE_USERNAME -e MQTT_DEVICE_PASSWORD \
  -e MQTT_SERVER_USERNAME -e MQTT_SERVER_PASSWORD \
  -v "${VOLUME_NAME}:/mosquitto/data" \
  eclipse-mosquitto:2.1.2 -c '
    set -eu
    mosquitto_passwd -b -c /mosquitto/data/password_file "$MQTT_DEVICE_USERNAME" "$MQTT_DEVICE_PASSWORD"
    mosquitto_passwd -b    /mosquitto/data/password_file "$MQTT_SERVER_USERNAME" "$MQTT_SERVER_PASSWORD"
    chmod 0600 /mosquitto/data/password_file
  '

echo "MQTT users created in Swarm volume ${VOLUME_NAME}."
