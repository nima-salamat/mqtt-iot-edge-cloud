#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
  echo "Missing .env. Copy .env.example and set all secrets first." >&2
  exit 1
fi
if grep -Eq 'REPLACE_|YOUR_|CHANGE_ME' .env; then
  echo "One or more placeholder values remain in .env." >&2
  exit 1
fi
if [ ! -s certs/fullchain.pem ] || [ ! -s certs/privkey.pem ]; then
  echo "Missing certs/fullchain.pem or certs/privkey.pem for MediaMTX RTSPS." >&2
  exit 1
fi

# Parse the Compose dotenv format safely; never execute .env as shell code.
source ./scripts/load_compose_env.sh
load_compose_env .env docker-compose.yml

: "${DOMAIN:?Set DOMAIN}"
: "${DJANGO_SECRET_KEY:?Set DJANGO_SECRET_KEY}"
: "${CSRF_TRUSTED_ORIGINS:?Set CSRF_TRUSTED_ORIGINS}"
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD}"
: "${PANEL_USERNAME:?Set PANEL_USERNAME}"
: "${PANEL_PASSWORD:?Set PANEL_PASSWORD}"
: "${MQTT_CAMERA_EDGE_USERNAME:?Set MQTT_CAMERA_EDGE_USERNAME}"
: "${MQTT_CAMERA_EDGE_PASSWORD:?Set MQTT_CAMERA_EDGE_PASSWORD}"
: "${MQTT_SERVER_USERNAME:?Set MQTT_SERVER_USERNAME}"
: "${MQTT_SERVER_PASSWORD:?Set MQTT_SERVER_PASSWORD}"

: "${STACK_NAME:=mqtt-iot}"
: "${DOCKER_REGISTRY:=docker.io}"
: "${IMAGE_TAG:=latest}"
if [[ ! "$DOCKER_REGISTRY" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
  echo "DOCKER_REGISTRY must be a registry host only, without https:// or a path." >&2
  exit 1
fi
: "${TRAEFIK_NETWORK:=traefik-public}"
: "${TRAEFIK_ENTRYPOINT:=websecure}"
: "${TRAEFIK_HTTP_ENTRYPOINT:=web}"
: "${TRAEFIK_CERT_RESOLVER:=letsencrypt}"
: "${POSTGRES_DB:=iotlab}"
: "${POSTGRES_USER:=iotlab}"
: "${MEDIAMTX_IMAGE:=${DOCKER_REGISTRY%/}/bluenviron/mediamtx:1.21.4}"
: "${MQTT_IOT_IMAGE:=${DOCKER_REGISTRY%/}/nima-salamat/mqtt-iot-edge-cloud:${IMAGE_TAG}}"
: "${MQTT_TOPIC_PREFIX:=iotlab}"
: "${MQTT_WS_PATH:=/mqtt}"
: "${DOMAIN:=server.echonode.website}"
: "${RTSPS_HOST:=$DOMAIN}"
export STACK_NAME DOCKER_REGISTRY IMAGE_TAG MQTT_IOT_IMAGE TRAEFIK_NETWORK TRAEFIK_ENTRYPOINT TRAEFIK_HTTP_ENTRYPOINT
export TRAEFIK_CERT_RESOLVER POSTGRES_DB POSTGRES_USER MEDIAMTX_IMAGE PROJECT_DIR
export MQTT_TOPIC_PREFIX MQTT_WS_PATH DOMAIN RTSPS_HOST

if [ "$(docker info --format '{{.Swarm.ControlAvailable}}')" != "true" ]; then
  echo "Run this deployment on a Docker Swarm manager node." >&2
  exit 1
fi
NETWORK_INFO="$(docker network inspect "$TRAEFIK_NETWORK" --format '{{.Driver}} {{.Scope}}' 2>/dev/null || true)"
if [ "$NETWORK_INFO" != "overlay swarm" ]; then
  echo "Traefik network '$TRAEFIK_NETWORK' must be an existing Swarm overlay network." >&2
  exit 1
fi

NODE_ID="$(docker info --format '{{.Swarm.NodeID}}')"
NODE_AVAILABILITY="$(docker node inspect "$NODE_ID" --format '{{.Spec.Availability}}')"
if [ "$NODE_AVAILABILITY" != "active" ]; then
  echo "The current manager node must have Active availability." >&2
  exit 1
fi
docker node update --label-add mqtt-iot-edge-cloud=true "$NODE_ID" >/dev/null

docker volume create "${STACK_NAME}_postgres_data" >/dev/null
docker volume create "${STACK_NAME}_mosquitto_data" >/dev/null
docker volume create "${STACK_NAME}_iot_media" >/dev/null
docker volume create "${STACK_NAME}_iot_static" >/dev/null
./scripts/create_mqtt_users_swarm.sh

docker build --build-arg DOCKER_REGISTRY="$DOCKER_REGISTRY" -t "$MQTT_IOT_IMAGE" .
docker stack deploy --resolve-image never -c docker-stack.traefik-swarm.yml "$STACK_NAME"

API_SERVICE="${STACK_NAME}_api"
LISTENER_SERVICE="${STACK_NAME}_listener"
READY=0
COUNT=0
while [ "$COUNT" -lt 120 ]; do
  API_CONTAINER="$(docker ps -q --filter "label=com.docker.swarm.service.name=$API_SERVICE" --filter status=running | head -n 1)"
  if [ -n "$API_CONTAINER" ]; then
    HEALTH="$(docker inspect "$API_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' 2>/dev/null || true)"
    if [ "$HEALTH" = "healthy" ]; then
      READY=1
      break
    fi
  fi
  COUNT=$((COUNT + 1))
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  echo "Django API did not become healthy. Inspect: docker service logs $API_SERVICE" >&2
  echo "The MQTT listener remains scaled to zero." >&2
  exit 1
fi

docker service scale "$LISTENER_SERVICE=1"
echo
echo "Swarm stack '$STACK_NAME' deployed."
echo "User panel: https://$DOMAIN/"
echo "Django Admin: https://$DOMAIN/admin/"
echo "Operator diagnostics: https://$DOMAIN/ops/"
echo "MQTT WSS: wss://$DOMAIN/mqtt"
echo "Camera RTSPS ingress: rtsps://<device-id>:<device-token>@$RTSPS_HOST:$RTSPS_PORT/<device-id>"
echo "Create first staff user inside the API task: python manage.py createsuperuser"
