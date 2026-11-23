#!/usr/bin/env sh
set -eu

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
  echo "Missing .env. Copy .env.example and set all passwords first." >&2
  exit 1
fi
if grep -Eq 'REPLACE_|YOUR_|CHANGE_ME' .env; then
  echo "One or more placeholder secrets remain in .env." >&2
  exit 1
fi

# Export the simple KEY=value entries so Docker Stack interpolation can read them.
set -a
. ./.env
set +a

: "${STACK_NAME:=mqtt-iot}"
: "${MQTT_IOT_IMAGE:=mqtt-iot-edge-cloud:local}"
: "${TRAEFIK_NETWORK:=traefik-public}"
: "${TRAEFIK_ENTRYPOINT:=websecure}"
: "${TRAEFIK_HTTP_ENTRYPOINT:=web}"
: "${TRAEFIK_CERT_RESOLVER:=letsencrypt}"
PROJECT_DIR="$PROJECT_DIR"
export STACK_NAME MQTT_IOT_IMAGE TRAEFIK_NETWORK TRAEFIK_ENTRYPOINT TRAEFIK_HTTP_ENTRYPOINT TRAEFIK_CERT_RESOLVER PROJECT_DIR

if [ "$(docker info --format '{{.Swarm.ControlAvailable}}')" != "true" ]; then
  echo "This mode must run on a Docker Swarm manager node." >&2
  exit 1
fi

NETWORK_INFO="$(docker network inspect "$TRAEFIK_NETWORK" --format '{{.Driver}} {{.Scope}}' 2>/dev/null || true)"
if [ "$NETWORK_INFO" != "overlay swarm" ]; then
  echo "Traefik network '$TRAEFIK_NETWORK' must be an existing Swarm overlay network (found: '$NETWORK_INFO')." >&2
  exit 1
fi

NODE_ID="$(docker info --format '{{.Swarm.NodeID}}')"
NODE_AVAILABILITY="$(docker node inspect "$NODE_ID" --format '{{.Spec.Availability}}')"
if [ "$NODE_AVAILABILITY" != "active" ]; then
  echo "The current Swarm manager node must have Active availability (found: '$NODE_AVAILABILITY')." >&2
  exit 1
fi
docker node update --label-add mqtt-iot-edge-cloud=true "$NODE_ID" >/dev/null

docker volume create "${STACK_NAME}_django_data" >/dev/null
docker volume create "${STACK_NAME}_static_data" >/dev/null
./scripts/create_mqtt_users_swarm.sh

docker build -t "$MQTT_IOT_IMAGE" .
docker stack deploy --resolve-image never -c docker-stack.traefik-swarm.yml "$STACK_NAME"

WEB_SERVICE="${STACK_NAME}_web"
LISTENER_SERVICE="${STACK_NAME}_listener"

# Wait until Django has completed migrations and its HTTP healthcheck is healthy.
READY=0
COUNT=0
while [ "$COUNT" -lt 90 ]; do
  WEB_CONTAINER="$(docker ps -q --filter "label=com.docker.swarm.service.name=${WEB_SERVICE}" --filter status=running | head -n 1)"
  if [ -n "$WEB_CONTAINER" ]; then
    HEALTH="$(docker inspect "$WEB_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' 2>/dev/null || true)"
    if [ "$HEALTH" = "healthy" ]; then
      READY=1
      break
    fi
  fi
  COUNT=$((COUNT + 1))
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  echo "Django did not become healthy. Listener remains scaled to zero." >&2
  echo "Inspect: docker service logs ${WEB_SERVICE}" >&2
  exit 1
fi

docker service scale "${LISTENER_SERVICE}=1"
echo
echo "Swarm stack '$STACK_NAME' deployed."
echo "Dashboard: https://${DOMAIN}"
echo "Listener: ${LISTENER_SERVICE} (1 replica)"
echo "Create the Django admin user by executing 'python manage.py createsuperuser' inside a running ${WEB_SERVICE} task."
