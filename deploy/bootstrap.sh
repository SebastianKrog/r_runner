#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-r_runner}"
APP_SERVICE="${APP_SERVICE:-r-runner}"
SITE_FILE="${SITE_FILE:-deploy/caddy/sites/r_runner.caddy}"
BASE_CADDYFILE="${BASE_CADDYFILE:-deploy/caddy/base/Caddyfile}"
SITE_DOMAIN="${SITE_DOMAIN:-localhost}"

SHARED_CADDY_CONTAINER="${SHARED_CADDY_CONTAINER:-shared-caddy}"
SHARED_PROXY_NETWORK="${SHARED_PROXY_NETWORK:-shared-proxy}"
SHARED_CADDY_DATA_VOLUME="${SHARED_CADDY_DATA_VOLUME:-shared-caddy-data}"
SHARED_CADDY_CONFIG_VOLUME="${SHARED_CADDY_CONFIG_VOLUME:-shared-caddy-config}"

if [[ ! -f "$SITE_FILE" ]]; then
  echo "Site snippet not found: $SITE_FILE" >&2
  exit 1
fi

if [[ ! -f "$BASE_CADDYFILE" ]]; then
  echo "Base Caddyfile not found: $BASE_CADDYFILE" >&2
  exit 1
fi

docker network inspect "$SHARED_PROXY_NETWORK" >/dev/null 2>&1 || docker network create "$SHARED_PROXY_NETWORK"
docker volume inspect "$SHARED_CADDY_DATA_VOLUME" >/dev/null 2>&1 || docker volume create "$SHARED_CADDY_DATA_VOLUME"
docker volume inspect "$SHARED_CADDY_CONFIG_VOLUME" >/dev/null 2>&1 || docker volume create "$SHARED_CADDY_CONFIG_VOLUME"

# Ensure the application container is up and attached to the shared proxy network.
docker compose up -d "$APP_SERVICE"

if ! docker ps -a --format '{{.Names}}' | grep -qx "$SHARED_CADDY_CONTAINER"; then
  docker run -d \
    --name "$SHARED_CADDY_CONTAINER" \
    --network "$SHARED_PROXY_NETWORK" \
    -p 80:80 \
    -p 443:443 \
    -p 2019:2019 \
    -v "$SHARED_CADDY_DATA_VOLUME:/data" \
    -v "$SHARED_CADDY_CONFIG_VOLUME:/etc/caddy" \
    caddy:2 \
    sh -c 'mkdir -p /etc/caddy/sites && printf "{\n    admin 0.0.0.0:2019\n}\n\nimport /etc/caddy/sites/*.caddy\n" > /etc/caddy/Caddyfile && caddy run --config /etc/caddy/Caddyfile --adapter caddyfile'
fi

# If caddy exists but is not running, restart it.
if ! docker ps --format '{{.Names}}' | grep -qx "$SHARED_CADDY_CONTAINER"; then
  docker start "$SHARED_CADDY_CONTAINER" >/dev/null
fi

docker cp "$BASE_CADDYFILE" "$SHARED_CADDY_CONTAINER:/etc/caddy/Caddyfile"

TMP_SITE_FILE="$(mktemp)"
sed "s|__SITE_DOMAIN__|${SITE_DOMAIN}|g" "$SITE_FILE" > "$TMP_SITE_FILE"
docker cp "$TMP_SITE_FILE" "$SHARED_CADDY_CONTAINER:/etc/caddy/sites/${APP_NAME}.caddy"
rm -f "$TMP_SITE_FILE"

docker exec "$SHARED_CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

echo "Bootstrap complete. ${APP_NAME} is registered in ${SHARED_CADDY_CONTAINER}."
