#!/usr/bin/env bash
# Redeploy the Market-Terminal freqtrade crypto sibling on the VPS.
#
# Idempotent and SAFE: it never overwrites your .env (that holds your secrets). It pulls the
# latest fork, (re)starts the bot from docker-compose.terminal-freqai.yml, ensures the Caddy
# site block exists, reloads Caddy, and verifies the endpoint. Respects whatever DRY_RUN is in
# your .env (it does not arm/disarm anything).
#
#   bash scripts/redeploy-terminal-freqai.sh
#
# Override any of these via env vars if your setup differs:
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/freqtrade}"
COMPOSE="docker-compose.terminal-freqai.yml"
DOMAIN="${FREQTRADE_DOMAIN:-freqtrade.noneborderboys.name}"
CADDY_CONTAINER="${CADDY_CONTAINER:-market-terminal-caddy-1}"
CADDY_SITES="${CADDY_SITES:-/opt/market-terminal/deploy/sites}"

echo "==> repo: $REPO_DIR"
cd "$REPO_DIR"

echo "==> pulling latest fork (develop)"
git fetch origin develop
git checkout develop
git pull --ff-only origin develop

echo "==> ensuring shared 'web' network exists"
docker network inspect web >/dev/null 2>&1 || docker network create web

if [ ! -f .env ]; then
  echo "!! .env is MISSING — creating it from the template. You MUST fill it in, then re-run:"
  cp .env.terminal-freqai.example .env
  echo "   edit $REPO_DIR/.env"
  echo "     - FREQTRADE__API_SERVER__USERNAME / PASSWORD / JWT_SECRET_KEY (openssl rand -hex 32)"
  echo "     - MT_API_URL (+ MT_API_TOKEN if your terminal is gated)"
  echo "     - leave exchange keys blank + FREQTRADE__DRY_RUN=true for paper"
  exit 1
fi

echo "==> (re)starting the bot"
docker compose -f "$COMPOSE" up -d --force-recreate

echo "==> ensuring the bot is on the 'web' network (so Caddy can reach it)"
docker network connect web terminal-freqai 2>/dev/null || true

echo "==> ensuring Caddy site block"
SITE_FILE="$CADDY_SITES/freqtrade.caddy"
if [ ! -f "$SITE_FILE" ]; then
  cat > "$SITE_FILE" <<EOF
$DOMAIN {
    reverse_proxy terminal-freqai:8080
}
EOF
  echo "   wrote $SITE_FILE"
fi
docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile \
  || echo "!! caddy reload failed — is the container named '$CADDY_CONTAINER'? (docker ps)"

echo "==> verifying (give DNS/cert a moment on first run)"
sleep 5
CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/api/v1/ping" || echo "000")
echo "   https://$DOMAIN/api/v1/ping -> $CODE"
if [ "$CODE" = "200" ]; then
  echo "==> DONE. FreqUI: https://$DOMAIN"
  echo "    Terminal -> Settings -> Crypto Bot: URL https://$DOMAIN + your api_server user/pass"
  DRY=$(grep -E '^FREQTRADE__DRY_RUN=' .env | cut -d= -f2 || echo "?")
  echo "    Mode: DRY_RUN=$DRY  (set to false + add exchange keys, then re-run, to go LIVE)"
else
  echo "!! not 200 yet — check logs: docker compose -f $COMPOSE logs --tail=40"
fi
