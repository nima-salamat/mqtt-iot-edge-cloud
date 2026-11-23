#!/usr/bin/env sh
set -eu

: "${STACK_NAME:=mqtt-iot}"
: "${MQTT_CAMERA_EDGE_USERNAME:?Set MQTT_CAMERA_EDGE_USERNAME}"
: "${MQTT_CAMERA_EDGE_PASSWORD:?Set MQTT_CAMERA_EDGE_PASSWORD}"
: "${MQTT_SERVER_USERNAME:?Set MQTT_SERVER_USERNAME}"
: "${MQTT_SERVER_PASSWORD:?Set MQTT_SERVER_PASSWORD}"

VOLUME_NAME="${STACK_NAME}_mosquitto_data"
docker volume create "$VOLUME_NAME" >/dev/null

docker run --rm --user root --entrypoint sh \
  -e MQTT_CAMERA_EDGE_USERNAME -e MQTT_CAMERA_EDGE_PASSWORD \
  -e MQTT_SERVER_USERNAME -e MQTT_SERVER_PASSWORD \
  -v "${VOLUME_NAME}:/mosquitto/data" \
  "${DOCKER_REGISTRY:-docker.io}/eclipse-mosquitto:2.1.2" -c '
    set -eu
    mosquitto_passwd -b -c /mosquitto/data/password_file "$MQTT_CAMERA_EDGE_USERNAME" "$MQTT_CAMERA_EDGE_PASSWORD"
    mosquitto_passwd -b    /mosquitto/data/password_file "$MQTT_SERVER_USERNAME" "$MQTT_SERVER_PASSWORD"
    chown 1883:1883 /mosquitto/data/password_file
    chmod 0640 /mosquitto/data/password_file
  '
echo "Mosquitto identities created in Swarm volume $VOLUME_NAME."
