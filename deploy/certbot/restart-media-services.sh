#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN="server.echonode.website"
PROJECT_DIR="${PROJECT_DIR:-/opt/mqtt-iot-edge-cloud}"

# Prefer the renewed certificate's SAN so this also works with older Certbot
# releases that don't export RENEWED_DOMAINS to deploy hooks.
RENEWED_CERT="${RENEWED_LINEAGE:-/etc/letsencrypt/live/$DOMAIN}/fullchain.pem"
if [[ -r "$RENEWED_CERT" ]]; then
  if ! openssl x509 -in "$RENEWED_CERT" -noout -checkhost "$DOMAIN" >/dev/null 2>&1; then
    exit 0
  fi
elif [[ " ${RENEWED_DOMAINS:-} " != *" ${DOMAIN} "* ]]; then
  exit 0
fi

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
