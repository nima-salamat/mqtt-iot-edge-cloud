#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN="server.echonode.website"
PROJECT_DIR="${PROJECT_DIR:-/opt/mqtt-iot-edge-cloud}"

# Certbot sets RENEWED_DOMAINS for deploy hooks. Ignore other certificates.
case " ${RENEWED_DOMAINS:-} " in
  *" ${DOMAIN} "*) ;;
  *) exit 0 ;;
esac

if [[ ! -d "$PROJECT_DIR" || ! -f "$PROJECT_DIR/.env" ]]; then
  echo "IoT project directory or .env not found: $PROJECT_DIR" >&2
  exit 1
fi

cd "$PROJECT_DIR"

if command -v nginx >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
  nginx -t
  systemctl reload nginx
fi

# The MediaMTX service definition is identical in SQLite and PostgreSQL modes.
# Use the same project directory and Compose project name as the main deploy.
if docker compose -f docker-compose.yml -f docker-compose.nginx.yml ps -q mediamtx | grep -q .; then
  docker compose -f docker-compose.yml -f docker-compose.nginx.yml restart mediamtx
fi
