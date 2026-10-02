#!/usr/bin/env bash
# Push the server-side pieces of tejstead.com: the landing page, the Caddy
# site snippets, and the site-pull script. Then reload Caddy and pull the
# latest math.tejstead.com build right away instead of waiting for cron.
#
# Site content for math.tejstead.com is not deployed from here. The
# heilbronn-site repo publishes each build to its rolling "site" release and
# server/site-pull.sh fetches it.
set -euo pipefail
cd "$(dirname "$0")"

# The deploy target lives outside the repo: set HOST=user@server, or put it
# in host.local (gitignored) once.
if [[ -z "${HOST:-}" && -f host.local ]]; then
    HOST="$(<host.local)"
fi
HOST="${HOST:?set HOST=user@server or create host.local}"

rsync -az landing/ "$HOST":/srv/www/tejstead/
rsync -az caddy/math.caddy caddy/tejstead.caddy "$HOST":/srv/caddy/sites/
rsync -az server/site-pull.sh "$HOST":bin/site-pull.sh

ssh "$HOST" 'cd /opt/elma \
  && docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile \
  && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile \
  && flock /tmp/site-pull.lock $HOME/bin/site-pull.sh'

echo "--- smoke checks ---"
curl -fsSI https://math.tejstead.com/heilbronn/ | head -1
curl -fsSI https://tejstead.com/ | head -1
curl -fsSI https://tejstead.com/resume | head -1
