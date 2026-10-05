#!/usr/bin/env bash
# Push the server-side pieces of tejstead.com: the landing page, the Caddy
# site snippets, the site-pull and visit-stats scripts (and visit-stats'
# cron entry). Then refresh the stats, reload Caddy, and pull the latest
# math.tejstead.com build right away instead of waiting for cron.
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
rsync -az server/visit-stats.py "$HOST":bin/visit-stats.py

# visit-stats runs every 10 minutes at the lowest CPU and disk priority, so
# it can never compete with Caddy during a traffic spike.
CRON='*/10 * * * * flock -n /tmp/visit-stats.lock nice -n 19 ionice -c 3 $HOME/bin/visit-stats.py >> $HOME/visit-stats.log 2>&1'
ssh "$HOST" "mkdir -p /srv/www/mathstats \
  && (crontab -l | grep -qF 'bin/visit-stats.py' || (crontab -l; echo '$CRON') | crontab -) \
  && flock /tmp/visit-stats.lock \$HOME/bin/visit-stats.py"

ssh "$HOST" 'cd /opt/elma \
  && docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile \
  && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile \
  && flock /tmp/site-pull.lock $HOME/bin/site-pull.sh'

echo "--- smoke checks ---"
curl -fsSI https://math.tejstead.com/heilbronn/ | head -1
curl -fsSI https://math.tejstead.com/stats/ | head -1
curl -fsSI https://tejstead.com/ | head -1
curl -fsSI https://tejstead.com/resume | head -1
