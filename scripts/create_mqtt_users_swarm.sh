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
  "${MOSQUITTO_IMAGE:-${DOCKER_REGISTRY:-docker.io}/eclipse-mosquitto:2.1.4-alpine}" -c '
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
echo "Mosquitto identities created in Swarm volume $VOLUME_NAME."
