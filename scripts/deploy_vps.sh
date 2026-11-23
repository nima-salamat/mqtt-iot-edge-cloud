#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
  echo "Copy .env.example to .env and set the secrets first." >&2
  exit 1
fi

set -a
. ./.env
set +a
PROXY_MODE="${PROXY_MODE:-traefik-swarm}"

# The host-Nginx Compose path uses host certificate paths, so it does not
# require the legacy ./certs/fullchain.pem and ./certs/privkey.pem files.
if [ "$PROXY_MODE" = "compose-nginx" ]; then
  exec bash ./scripts/deploy_compose_nginx.sh
fi

if grep -Eq 'REPLACE_|YOUR_|CHANGE_ME' .env; then
  echo "One or more placeholder values remain in .env." >&2
  exit 1
fi
if [ ! -s certs/fullchain.pem ] || [ ! -s certs/privkey.pem ]; then
  echo "Missing certs/fullchain.pem or certs/privkey.pem for MediaMTX RTSPS." >&2
  echo "See certs/README.md. Do not commit the private key." >&2
  exit 1
fi

case "$PROXY_MODE" in
  traefik-swarm)
    ./scripts/deploy_vps_swarm.sh
    ;;
  traefik-compose)
    ./scripts/create_mqtt_users.sh
    TRAEFIK_NETWORK="${TRAEFIK_NETWORK:-traefik-public}"
    if ! docker network inspect "$TRAEFIK_NETWORK" >/dev/null 2>&1; then
      echo "Traefik Docker network '$TRAEFIK_NETWORK' was not found." >&2
      exit 1
    fi
    docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
    ;;
  standalone)
    ./scripts/create_mqtt_users.sh
    docker compose --profile standalone up -d --build
    ;;
  *)
    echo "Unsupported PROXY_MODE='$PROXY_MODE'. Use compose-nginx, traefik-swarm, traefik-compose or standalone." >&2
    exit 1
    ;;
esac

echo
echo "User dashboard: https://${DOMAIN:-server.echonode.website}/"
echo "Django Admin: https://${DOMAIN:-server.echonode.website}/admin/"
echo "Operator diagnostics: https://${DOMAIN:-server.echonode.website}/ops/"
echo "Create the first staff user with: docker compose exec api python manage.py createsuperuser"
