#!/usr/bin/env bash
# Pull-based deploy: fetch the latest site build published by the
# publish-site workflow (rolling "site" release) and sync it into
# /srv/www/math. Runs on the box from cron every 5 minutes:
#
#   */5 * * * * flock -n /tmp/site-pull.lock $HOME/bin/site-pull.sh >> $HOME/site-pull.log 2>&1
#
# Quiet unless it deploys (or fails), so the log only records real events.
# No credentials involved: release assets on a public repo are anonymous
# downloads over HTTPS. Until the repo/release exists, exits silently.
set -euo pipefail

REPO="tejstead/heilbronn-site"
BASE="https://github.com/$REPO/releases/download/site"
DEST="/srv/www/math"
STATE="$HOME/.site-pull.sha"

new=$(curl -fsSL --max-time 30 "$BASE/site.sha" 2>/dev/null) || exit 0
[[ "$new" =~ ^[0-9a-f]{40}$ ]] || exit 0
[[ -f "$STATE" && "$(cat "$STATE")" == "$new" ]] && exit 0

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

curl -fsSL --max-time 300 -o "$work/site.tar.gz" "$BASE/site.tar.gz"
mkdir "$work/dist"
tar -xzf "$work/site.tar.gz" -C "$work/dist"

# sanity: never wipe the live site with a broken tarball
[[ -f "$work/dist/heilbronn/index.html" ]] || { echo "$(date -u +%FT%TZ) bad tarball for $new — not deploying"; exit 1; }

rsync -a --delete "$work/dist/" "$DEST"/
echo "$new" > "$STATE"
echo "$(date -u +%FT%TZ) deployed $new"
