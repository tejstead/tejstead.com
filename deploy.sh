#!/usr/bin/env bash
# Build locally, rsync to the box, reload Caddy. The server never builds or
# computes anything — it only serves what this script puts under /srv/www.
#
# SKIP_BUILD=1 deploys whatever is already in dist/ (used for the first
# hello-world deploy and for redeploying an unchanged build).
set -euo pipefail
cd "$(dirname "$0")/.."

# The deploy target lives outside the repo: set HOST=user@server, or put it
# in deploy/host.local (gitignored) once.
if [[ -z "${HOST:-}" && -f deploy/host.local ]]; then
    HOST="$(<deploy/host.local)"
fi
HOST="${HOST:?set HOST=user@server or create deploy/host.local}"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    .venv/bin/python -m build
fi

[[ -f dist/heilbronn/index.html ]] || { echo "dist/ is empty — build first" >&2; exit 1; }

# --delete: this repo owns all of /srv/www/math. A future second site under
# math.tejstead.com must live inside this dist, or in its own /srv/www/<name>.
rsync -az --delete dist/ "$HOST":/srv/www/math/
rsync -az landing/ "$HOST":/srv/www/tejstead/
rsync -az deploy/math.caddy deploy/tejstead.caddy "$HOST":/srv/caddy/sites/

ssh "$HOST" 'cd /opt/elma \
  && docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile \
  && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile'

echo "--- smoke checks ---"
curl -fsSI https://math.tejstead.com/heilbronn/ | head -3
curl -fsSI https://tejstead.com/ | head -1 \
  || echo "(apex not serving yet — expected until its DNS points at this box)"
