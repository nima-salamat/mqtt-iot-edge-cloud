#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ -f .env ]] || fail "Missing .env. Start with: cp .env.example .env"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is not available."
command -v openssl >/dev/null 2>&1 || fail "openssl is required for certificate checks."
command -v curl >/dev/null 2>&1 || fail "curl is required for the API health check."

set -a
# shellcheck disable=SC1091
. ./.env
set +a

DOMAIN="${DOMAIN:-server.echonode.website}"
DATABASE_MODE="${DATABASE_MODE:-postgres}"
HOST_CERT_DIR="${HOST_CERT_DIR:-}"
MEDIA_TLS_CERT="${MEDIA_TLS_CERT:-}"
MEDIA_TLS_KEY="${MEDIA_TLS_KEY:-}"
API_BIND_PORT="${API_BIND_PORT:-18000}"
OPS_BIND_PORT="${OPS_BIND_PORT:-18501}"
MQTT_WS_BIND_PORT="${MQTT_WS_BIND_PORT:-19001}"

case "$DATABASE_MODE" in
  sqlite|postgres) ;;
  *) fail "DATABASE_MODE must be 'sqlite' or 'postgres', not '$DATABASE_MODE'." ;;
esac

for name in DJANGO_SECRET_KEY PANEL_USERNAME PANEL_PASSWORD \
            MQTT_CAMERA_EDGE_USERNAME MQTT_CAMERA_EDGE_PASSWORD \
            MQTT_SERVER_USERNAME MQTT_SERVER_PASSWORD; do
  value="${!name:-}"
  [[ -n "$value" ]] || fail "$name is missing in .env."
  case "$value" in
    *REPLACE_*|YOUR_*|CHANGE_ME*) fail "$name still contains a placeholder." ;;
  esac
done

if [[ "$DATABASE_MODE" == "postgres" ]]; then
  [[ -n "${POSTGRES_PASSWORD:-}" ]] || fail "Set POSTGRES_PASSWORD for PostgreSQL mode."
  case "$POSTGRES_PASSWORD" in
    *REPLACE_*|YOUR_*|CHANGE_ME*) fail "POSTGRES_PASSWORD still contains a placeholder." ;;
  esac
fi

[[ -n "$HOST_CERT_DIR" && -d "$HOST_CERT_DIR" ]] || \
  fail "HOST_CERT_DIR must point to the host directory containing the TLS certificate and key."

container_cert_to_host() {
  local container_path="$1"
  case "$container_path" in
    /host-certs/*) printf '%s/%s' "${HOST_CERT_DIR%/}" "${container_path#/host-certs/}" ;;
    *) fail "MEDIA_TLS_CERT and MEDIA_TLS_KEY must use paths below /host-certs/." ;;
  esac
}

CERT_FILE="$(container_cert_to_host "$MEDIA_TLS_CERT")"
KEY_FILE="$(container_cert_to_host "$MEDIA_TLS_KEY")"
[[ -r "$CERT_FILE" ]] || fail "MediaMTX certificate is not readable: $CERT_FILE"
[[ -r "$KEY_FILE" ]] || fail "MediaMTX private key is not readable: $KEY_FILE"

openssl x509 -in "$CERT_FILE" -noout -checkhost "$DOMAIN" >/dev/null 2>&1 || \
  fail "The selected certificate does not validate for $DOMAIN: $CERT_FILE"
openssl x509 -in "$CERT_FILE" -noout -checkend 0 >/dev/null 2>&1 || \
  fail "The selected certificate is expired: $CERT_FILE"
openssl pkey -in "$KEY_FILE" -noout >/dev/null 2>&1 || \
  fail "Could not read the MediaMTX private key: $KEY_FILE"

CERT_PUB="$(openssl x509 -in "$CERT_FILE" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')"
KEY_PUB="$(openssl pkey -in "$KEY_FILE" -pubout -outform DER 2>/dev/null | sha256sum | awk '{print $1}')"
[[ -n "$CERT_PUB" && "$CERT_PUB" == "$KEY_PUB" ]] || \
  fail "The certificate and private key do not match."

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.nginx.yml)
if [[ "$DATABASE_MODE" == "sqlite" ]]; then
  COMPOSE_FILES+=(-f docker-compose.sqlite.yml)
fi

compose() {
  docker compose "${COMPOSE_FILES[@]}" "$@"
}

echo "Validating Docker Compose configuration (database: $DATABASE_MODE)..."
compose config -q

echo "Preparing Mosquitto credentials..."
./scripts/create_mqtt_users.sh

echo "Building and starting services..."
compose up -d --build

echo "Waiting for Django API health..."
API_READY=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$API_BIND_PORT/health/" >/dev/null 2>&1; then
    API_READY=1
    break
  fi
  sleep 2
done

if [[ "$API_READY" != "1" ]]; then
  compose ps
  compose logs --tail=120 api
  fail "Django did not become healthy. Review the API logs above."
fi

echo
compose ps
echo
echo "Django dashboard: https://$DOMAIN/"
echo "Django Admin:     https://$DOMAIN/admin/"
echo "Operator panel:   https://$DOMAIN/ops/"
echo "MQTT over WSS:    wss://$DOMAIN/mqtt"
echo "RTSPS:            rtsps://$DOMAIN:8322"
echo "WebRTC ICE:       UDP $DOMAIN:8189"
echo
echo "Create the first administrator with:"
echo "  docker compose ${COMPOSE_FILES[*]} exec api python manage.py createsuperuser"
echo
echo "Install deploy/nginx/server.echonode.website.conf in the host Nginx sites-enabled directory,"
echo "then run nginx -t and reload Nginx. Certbot renewals reload Nginx and restart MediaMTX via the deploy hook."
