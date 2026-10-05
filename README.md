# tejstead.com

Server setup for tejstead.com and its subdomains: the landing page, the
Caddy site configs, and the script that pulls math.tejstead.com builds.

| Path | Contents |
| --- | --- |
| `landing/` | The tejstead.com front page and résumé (served at `/resume`). |
| `caddy/tejstead.caddy` | The apex, `www`, and an HTTP-only redirect for unknown subdomains. |
| `caddy/math.caddy` | math.tejstead.com, serving builds of [heilbronn-site](https://github.com/tejstead/heilbronn-site). |
| `caddy/Caddyfile.local` | Local preview of a heilbronn-site build with the same file serving as `math.caddy`. |
| `server/site-pull.sh` | Runs from cron on the server. Fetches the latest heilbronn-site build from its `site` release. |
| `server/visit-stats.py` | Runs from cron on the server. Turns Caddy's access log for math.tejstead.com into the aggregate counts at [/stats/](https://math.tejstead.com/stats/). |
| `deploy.sh` | Syncs all of the above to the server, reloads Caddy, and pulls the latest build. |

## How the server is set up

Caddy runs in Docker from the elma repo (`/opt/elma`), whose main Caddyfile
imports `/etc/caddy/sites/*.caddy`. That directory is bind-mounted from
`/srv/caddy/sites`, and site files live under `/srv/www`. Wildcard DNS
sends every `*.tejstead.com` subdomain to the box. Subdomains with a site
block get their own certificate over HTTP-01, so no registrar API key is
needed.

math.tejstead.com updates itself. Every push to heilbronn-site's `main`
builds a tarball onto that repo's `site` release, and cron on the server
runs `~/bin/site-pull.sh` every five minutes:

```
*/5 * * * * flock -n /tmp/site-pull.lock $HOME/bin/site-pull.sh >> $HOME/site-pull.log 2>&1
```

Visitor stats work the same way, from cron every ten minutes:

```
*/10 * * * * flock -n /tmp/visit-stats.lock nice -n 19 ionice -c 3 $HOME/bin/visit-stats.py >> $HOME/visit-stats.log 2>&1
```

It reads only the log lines it hasn't seen and keeps nothing but counts:
page views, daily and hourly unique visitors, pages, referrer domains,
browser, OS and device class. Nothing runs in visitors' browsers and no
cookies are set. Unique visitors use a hash of IP and browser string with a
salt that is thrown away at the end of each UTC day; the script's docstring
has the details. The output goes to `/srv/www/mathstats/stats/`, outside
`/srv/www/math`, which site-pull.sh replaces on every deploy.

## Commands

```
make deploy                                      # push landing, Caddy configs, scripts
make serve-math DIST=../heilbronn-site/dist      # preview a build on :8081
```

`deploy.sh` reads the target from `HOST=user@server` or from `host.local`
(gitignored).

To get a heilbronn-site change live immediately: run
`gh workflow run publish-site -R tejstead/heilbronn-site`, wait for it to
finish, then `make deploy`.
