#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
  echo "Copy .env.example to .env, set strong secrets and verify DOMAIN first." >&2
  exit 1
fi

if [ ! -f mosquitto/aclfile ]; then
  echo "Missing Mosquitto ACL file." >&2
  exit 1
fi

if grep -Eq 'REPLACE_|YOUR_|CHANGE_ME' .env; then
  echo "One or more placeholder secrets remain in .env. Replace them before deployment." >&2
  exit 1
fi

# Read the deployment mode from .env as well as the Docker Compose variables.
set -a
. ./.env
set +a
PROXY_MODE="${PROXY_MODE:-traefik-swarm}"
case "$PROXY_MODE" in
  traefik-swarm)
    ./scripts/deploy_vps_swarm.sh
    ;;
  traefik-compose)
    ./scripts/create_mqtt_users.sh
    TRAEFIK_NETWORK="${TRAEFIK_NETWORK:-traefik-public}"
    if ! docker network inspect "$TRAEFIK_NETWORK" >/dev/null 2>&1; then
      echo "Traefik Docker network '$TRAEFIK_NETWORK' was not found." >&2
      echo "Set TRAEFIK_NETWORK in .env to the existing shared network, or use PROXY_MODE=standalone." >&2
      exit 1
    fi
    docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
    ;;
  standalone)
    ./scripts/create_mqtt_users.sh
    docker compose --profile standalone up -d --build
    ;;
  *)
    echo "Unsupported PROXY_MODE='$PROXY_MODE'. Use traefik-swarm, traefik-compose or standalone." >&2
    exit 1
    ;;
esac

echo
echo "Cloud dashboard: https://${DOMAIN:-server.echonode.website}"
if [ "$PROXY_MODE" = "traefik-swarm" ]; then
  echo "Create the dashboard admin user inside a running web task: docker ps --filter label=com.docker.swarm.service.name=${STACK_NAME}_web, then docker exec -it <CONTAINER_ID> python manage.py createsuperuser."
else
  echo "Next: run 'docker compose exec web python manage.py createsuperuser' to create dashboard login."
fi
