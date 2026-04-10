#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-r_runner}"
APP_SERVICE="${APP_SERVICE:-r-runner}"

SHARED_CADDY_CONTAINER="${SHARED_CADDY_CONTAINER:-shared-caddy}"
STOP_SHARED_CADDY_IF_EMPTY="${STOP_SHARED_CADDY_IF_EMPTY:-false}"

docker compose stop "$APP_SERVICE" >/dev/null || true

if docker ps -a --format '{{.Names}}' | grep -qx "$SHARED_CADDY_CONTAINER"; then
  docker exec "$SHARED_CADDY_CONTAINER" rm -f "/etc/caddy/sites/${APP_NAME}.caddy"
  docker exec "$SHARED_CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

  if [[ "$STOP_SHARED_CADDY_IF_EMPTY" == "true" ]]; then
    SITE_COUNT="$(docker exec "$SHARED_CADDY_CONTAINER" sh -c 'ls -1 /etc/caddy/sites/*.caddy 2>/dev/null | wc -l')"
    if [[ "$SITE_COUNT" == "0" ]]; then
      docker stop "$SHARED_CADDY_CONTAINER" >/dev/null
    fi
  fi
fi

echo "Teardown complete for ${APP_NAME}."
