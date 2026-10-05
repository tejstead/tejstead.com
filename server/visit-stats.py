#!/usr/bin/env python3
"""Aggregate visitor statistics for math.tejstead.com from Caddy's access log.

Runs from cron every 10 minutes (deploy.sh installs it as ~/bin/visit-stats.py):

    */10 * * * * flock -n /tmp/visit-stats.lock $HOME/bin/visit-stats.py >> $HOME/visit-stats.log 2>&1

Nothing runs in the visitor's browser: no cookies, no JavaScript beacon, no
third parties. The input is the log Caddy already writes. Each run reads the
lines it hasn't seen yet and adds them to counts per hour and per day (page
views, unique visitors, pages, referrer domains, browser, OS, device class).
Only those counts are kept; the page at math.tejstead.com/stats/ and its
stats.json are generated from them.

Unique visitors are counted the way Plausible and GoatCounter do it: a hash
of (random daily salt, IP, User-Agent), held only to tell whether that
visitor was already counted today. When the UTC day ends, the salt and the
hashes are deleted, so visitors can't be linked across days, and the stored
history holds no IPs, hashes or browser strings — just numbers.

Stdlib only. For a backfill from saved log files, run it with --logs FILE...
"""

import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import zlib
from collections import Counter, namedtuple
from pathlib import Path
from urllib.parse import urlsplit

HOST = "math.tejstead.com"
COMPOSE = "/opt/elma/docker-compose.yml"
STATE = Path.home() / "visit-stats" / "state.json"   # private: holds today's salt
OUT = Path("/srv/www/mathstats/stats")                # served at /stats/
KEEP_HOURS = 24 * 90

# Browsers announce themselves as Mozilla/...; everything else (link-preview
# fetchers, HN apps' native clients, feed readers, scripts) is not a view.
BOT = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternal|embedly|"
                 r"headless|lighthouse|feed|rss|monitor|uptime|scan", re.I)
ROTATED = re.compile(r"access-math-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d\.\d+)-size\.log\.gz$")


# ---------------------------------------------------------------- reading

def caddy_logs(hwm):
    """Lines of every access-math log inside the Caddy container, streamed
    through one tar so a rotation can't slip between listing and reading.
    Rotated files that ended before the high-water mark are skipped unread."""
    proc = subprocess.Popen(
        ["docker", "compose", "-f", COMPOSE, "exec", "-T", "caddy",
         "sh", "-c", "cd /data && tar cf - access-math*"],
        stdout=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
        for m in tf:
            rot = ROTATED.match(m.name)
            if rot:
                ended = dt.datetime.strptime(rot.group(1), "%Y-%m-%dT%H-%M-%S.%f")
                if ended.replace(tzinfo=dt.UTC).timestamp() < hwm:
                    continue
            yield from stream_lines(tf.extractfile(m), m.name.endswith(".gz"))
    if proc.wait():
        raise SystemExit(f"reading the Caddy logs failed (exit {proc.returncode})")


def stream_lines(f, gzipped, chunk=1 << 20):
    """Lines from a forward-only file object, decompressing as it goes. The
    streamed tar's members can't be wrapped by io or gzip (no seekable())."""
    z = zlib.decompressobj(wbits=31) if gzipped else None
    rest = b""
    while data := f.read(chunk):
        if z:
            data = z.decompress(data)
        lines = (rest + data).split(b"\n")
        rest = lines.pop()
        for line in lines:
            yield line.decode("utf-8", "replace")
    if rest:
        yield rest.decode("utf-8", "replace")


def file_logs(paths):
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt", encoding="utf-8", errors="replace") as f:
            yield from f


# Only these fields of a log entry are kept in memory: a run can see ~60 MB
# of log during a traffic spike, and whole parsed entries cost ~8 KB each.
Hit = namedtuple("Hit", "ts port ip host method uri status ua ref")


def hits(lines, hwm):
    """Page requests newer than hwm, de-duplicated and in time order."""
    seen, out = set(), []
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue   # a line Caddy is still writing
        if e.get("ts", 0) <= hwm:
            continue
        req = e["request"]
        h = req.get("headers", {})
        hit = Hit(e["ts"], req.get("remote_port"), req.get("client_ip"), req.get("host"),
                  req.get("method"), req.get("uri", "").split("?")[0], e.get("status"),
                  (h.get("User-Agent") or [""])[0], (h.get("Referer") or [""])[0])
        key = (hit.ts, hit.port, hit.uri)
        if is_page(hit) and key not in seen:
            seen.add(key)
            out.append(hit)
    out.sort(key=lambda x: x.ts)
    return out


# ---------------------------------------------------------------- classifying

def is_page(hit):
    return (hit.method == "GET" and hit.status in (200, 304) and hit.host == HOST
            and not hit.uri.startswith("/stats")
            and (hit.uri.endswith("/") or hit.uri.endswith(".html")))


def is_browser(ua):
    return ua.startswith("Mozilla/") and not BOT.search(ua)


def browser(ua):
    for name, pat in (("Edge", "Edg/"), ("Opera", "OPR/"), ("Samsung Internet", "SamsungBrowser"),
                      ("Firefox", r"Firefox/|FxiOS"), ("Chrome", r"Chrome/|CriOS"),
                      ("Safari", "Safari/")):
        if re.search(pat, ua):
            return name
    return "Other"


def system(ua):
    for name, pat in (("iOS", r"iPhone|iPad|iPod"), ("Android", "Android"),
                      ("ChromeOS", "CrOS"), ("macOS", "Mac OS X"),
                      ("Windows", "Windows"), ("Linux", "Linux")):
        if re.search(pat, ua):
            return name
    return "Other"


def device(ua):
    if re.search(r"iPad|Tablet", ua):
        return "Tablet"
    if re.search(r"Mobi|iPhone|Android", ua):
        return "Mobile"
    return "Desktop"


def referrer(ref):
    if not ref:
        return "(direct)"
    host = (urlsplit(ref).hostname or ref).lower()
    host = host.removeprefix("www.")
    return None if host == HOST else host   # internal navigation isn't a source


# ---------------------------------------------------------------- counting

def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.UTC)


def new_day():
    return {"views": 0, "visitors": 0, "bot_hits": 0, "pages": {}, "referrers": {},
            "browsers": {}, "systems": {}, "devices": {}}


def bump(d, key, n=1):
    d[key] = d.get(key, 0) + n


def tally(state, events):
    today = state.get("today")
    if today:   # sets while counting, lists on disk
        today["seen"], today["seen_hour"] = set(today["seen"]), set(today["seen_hour"])
    for hit in events:
        t = utc(hit.ts)
        day, hour = t.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%dT%H")
        ua = hit.ua
        d = state["days"].setdefault(day, new_day())
        if not is_browser(ua):
            d["bot_hits"] += 1
            continue
        if today is None or today["day"] != day:
            # New UTC day: yesterday's salt and hashes are dropped here.
            today = {"day": day, "salt": secrets.token_hex(16), "seen": set(),
                     "hour": hour, "seen_hour": set()}
        if today["hour"] != hour:
            today["hour"], today["seen_hour"] = hour, set()
        h = hashlib.sha256(f"{today['salt']}|{hit.ip}|{ua}"
                           .encode()).hexdigest()[:16]
        hr = state["hours"].setdefault(hour, [0, 0])
        hr[0] += 1
        d["views"] += 1
        if h not in today["seen"]:
            today["seen"].add(h)
            d["visitors"] += 1
        if h not in today["seen_hour"]:
            today["seen_hour"].add(h)
            hr[1] += 1
        bump(d["pages"], hit.uri)
        ref = referrer(hit.ref)
        if ref:
            bump(d["referrers"], ref)
        bump(d["browsers"], browser(ua))
        bump(d["systems"], system(ua))
        bump(d["devices"], device(ua))
    # A finished day's salt goes even if no visitor has arrived since midnight.
    now = dt.datetime.now(dt.UTC)
    if today and today["day"] != now.strftime("%Y-%m-%d"):
        today = None
    if today:
        today["seen"], today["seen_hour"] = sorted(today["seen"]), sorted(today["seen_hour"])
    state["today"] = today
    cutoff = (now - dt.timedelta(hours=KEEP_HOURS)).strftime("%Y-%m-%dT%H")
    state["hours"] = {k: v for k, v in sorted(state["hours"].items()) if k >= cutoff}


# ---------------------------------------------------------------- output

def write_atomic(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def public(state):
    return {"site": HOST, "updated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "since": utc(state["since"]).isoformat(timespec="seconds") if state.get("since") else None,
            "note": "visitors are distinct per day (and per hour); they can't be summed into "
                    "unique visitors over longer periods",
            "hours": state["hours"], "days": state["days"]}


def merge(days, field):
    c = Counter()
    for d in days:
        c.update(d[field])
    return c


def fmt(n):
    return f"{n:,}"


def nice_scale(v):
    """(top, step) for a y-axis: step is 1, 2 or 5 x 10^k, with at most 5 steps."""
    k = 1
    while True:
        for m in (1, 2, 5):
            step = m * k
            if v <= 5 * step:
                return max(step, step * -(-v // step)), step
        k *= 10


def short(n):
    """Axis label: 2,500 -> 2.5k."""
    if n >= 1000:
        return f"{n / 1000:g}k"
    return str(int(n))


def bar_chart(points, label_every, label_fmt, aria):
    """Single-series bar chart as inline SVG: points = [(key, views, visitors)]."""
    W, H, L, B, T = 760, 228, 44, 34, 10
    n = len(points)
    top, step = nice_scale(max((p[1] for p in points), default=0))
    pw, ph = W - L - 8, H - T - B
    slot = pw / max(n, 1)
    bw = max(1.0, slot - 2)                      # 2px surface gap between bars
    r = min(4, bw / 2)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(aria)}">']
    v = 0
    while v <= top + 1e-9:
        y = T + ph - ph * v / top
        out.append(f'<line class="grid" x1="{L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="tick" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{short(v)}</text>')
        v += step
    for i, (key, views, visitors) in enumerate(points):
        x = L + i * slot + (slot - bw) / 2
        h = ph * views / top
        y0 = T + ph
        if h > 0:
            rr = min(r, h)
            out.append(f'<path class="bar" d="M{x:.1f},{y0} V{y0 - h + rr:.1f} '
                       f'Q{x:.1f},{y0 - h:.1f} {x + rr:.1f},{y0 - h:.1f} H{x + bw - rr:.1f} '
                       f'Q{x + bw:.1f},{y0 - h:.1f} {x + bw:.1f},{y0 - h + rr:.1f} V{y0}Z"/>')
        tip = f"{label_fmt(key, True)}: {fmt(views)} views, {fmt(visitors)} visitors"
        out.append(f'<rect class="hit" x="{L + i * slot:.1f}" y="{T}" width="{slot:.1f}" '
                   f'height="{ph}" data-tip="{html.escape(tip)}"><title>{html.escape(tip)}</title></rect>')
        if i % label_every == 0:
            out.append(f'<text class="tick" x="{x + bw / 2:.1f}" y="{H - 4}" '
                       f'text-anchor="middle">{label_fmt(key, False)}</text>')
    out.append(f'<line class="axis" x1="{L}" x2="{W - 8}" y1="{T + ph}" y2="{T + ph}"/></svg>')
    return "".join(out)


def top_table(title, counter, total, limit=12):
    rows = []
    for name, n in counter.most_common(limit):
        pct = 100 * n / total if total else 0
        rows.append(f'<tr><td class="name" title="{html.escape(name)}">{html.escape(name)}</td>'
                    f'<td class="num">{fmt(n)}</td>'
                    f'<td class="meter"><span style="width:{pct:.1f}%"></span></td></tr>')
    if not rows:
        rows.append('<tr><td class="name muted">none yet</td><td></td><td></td></tr>')
    more = len(counter) - limit
    foot = f'<p class="more">and {more} more</p>' if more > 0 else ""
    return (f'<section class="panel"><h3>{title}</h3><table>{"".join(rows)}</table>{foot}</section>')


def render(state):
    now = dt.datetime.now(dt.UTC)
    days = state["days"]
    day_keys = sorted(days)

    def span(n):
        keys = [(now - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
        return [days[k] for k in keys if k in days]

    def tile(label, ds, visitors_label):
        views = sum(d["views"] for d in ds)
        vis = sum(d["visitors"] for d in ds)
        return (f'<div class="tile"><div class="tile-label">{label}</div>'
                f'<div class="tile-value">{fmt(views)}</div><div class="tile-sub">page views</div>'
                f'<div class="tile-value small">{fmt(vis)}</div>'
                f'<div class="tile-sub">{visitors_label}</div></div>')

    today_k = now.strftime("%Y-%m-%d")
    yest_k = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    tiles = "".join([
        tile("Today (UTC)", [days[today_k]] if today_k in days else [], "visitors"),
        tile("Yesterday", [days[yest_k]] if yest_k in days else [], "visitors"),
        tile("Last 7 days", span(7), "daily visitors, summed"),
        tile("Last 30 days", span(30), "daily visitors, summed"),
        tile("All time", list(days.values()), "daily visitors, summed"),
    ])

    hours = []
    for i in range(47, -1, -1):
        k = (now - dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        v = state["hours"].get(k, [0, 0])
        hours.append((k, v[0], v[1]))
    hour_chart = bar_chart(
        hours, 6,
        lambda k, full: (f"{k[:10]} {k[11:]}:00 UTC" if full else f"{k[11:]}:00"),
        "Page views per hour, last 48 hours")

    first = dt.date.fromisoformat(day_keys[0]) if day_keys else now.date()
    n_days = min(90, max(14, (now.date() - first).days + 1))
    daily = []
    for i in range(n_days - 1, -1, -1):
        k = (now - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        d = days.get(k)
        daily.append((k, d["views"] if d else 0, d["visitors"] if d else 0))
    day_chart = bar_chart(
        daily, -(-n_days // 7),
        lambda k, full: (k if full else dt.date.fromisoformat(k).strftime("%b %-d")),
        f"Page views per day, last {n_days} days")

    last30 = span(30)
    views30 = sum(d["views"] for d in last30)
    panels = "".join([
        top_table("Pages", merge(last30, "pages"), views30),
        top_table("Referrers", merge(last30, "referrers"), views30),
        top_table("Browsers", merge(last30, "browsers"), views30),
        top_table("Operating systems", merge(last30, "systems"), views30),
        top_table("Devices", merge(last30, "devices"), views30),
    ])
    bots30 = sum(d["bot_hits"] for d in last30)
    since = (utc(state["since"]).strftime("%Y-%m-%d %H:%M UTC") if state.get("since")
             else "now")
    return PAGE.format(updated=now.strftime("%Y-%m-%d %H:%M UTC"), since=since, tiles=tiles,
                       hour_chart=hour_chart, day_chart=day_chart, panels=panels,
                       n_days=n_days, bots30=fmt(bots30))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Visitor stats</title>
<meta name="description" content="Aggregate, cookie-free visitor counts for math.tejstead.com.">
<link rel="icon" href="/heilbronn/favicon.svg" type="image/svg+xml">
<style>
:root {{
  color-scheme: light dark;
  --bg: #fdfcfa; --fg: #1c1c22; --muted: #6a6a72; --line: #e4e2dc; --card: #ffffff;
  --grid: #ece9e3; --bar: #2a78d6; --meter: #2a78d6; --meter-track: #f0eee9;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #16161a; --fg: #e8e6e1; --muted: #97959e; --line: #2c2c33; --card: #1e1e24;
    --grid: #26262c; --bar: #3987e5; --meter: #3987e5; --meter-track: #26262c;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg);
  font-family: Charter, Georgia, "Times New Roman", serif; line-height: 1.55; }}
main {{ max-width: 980px; margin: 0 auto; padding: 28px 16px 48px; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
h2 {{ font-size: 1.15rem; margin: 32px 0 8px; }}
h3 {{ font-family: var(--sans); font-size: .78rem; text-transform: uppercase;
  letter-spacing: .05em; color: var(--muted); margin: 0 0 8px; font-weight: 600; }}
.sub, .muted, .more, .note {{ color: var(--muted); }}
.sub {{ margin: 0 0 20px; font-family: var(--sans); font-size: .88rem; }}
a {{ color: inherit; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
.tile, .panel {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }}
.tile-label {{ font-family: var(--sans); font-size: .78rem; color: var(--muted); margin-bottom: 4px; }}
.tile-value {{ font-family: var(--sans); font-size: 1.6rem; font-weight: 600;
  font-variant-numeric: tabular-nums; line-height: 1.15; }}
.tile-value.small {{ font-size: 1.05rem; margin-top: 8px; }}
.tile-sub {{ font-family: var(--sans); font-size: .75rem; color: var(--muted); }}
.chart-wrap {{ position: relative; }}
.chart {{ width: 100%; height: auto; display: block; font-family: var(--sans); }}
.chart .grid {{ stroke: var(--grid); stroke-width: 1; }}
.chart .axis {{ stroke: var(--muted); stroke-width: 1; opacity: .6; }}
.chart .tick {{ fill: var(--muted); font-size: 11px; }}
.chart .bar {{ fill: var(--bar); }}
@media (max-width: 600px) {{ .chart .tick {{ font-size: 20px; }} }}
.chart .hit {{ fill: transparent; }}
.chart .hit:hover {{ fill: var(--fg); opacity: .06; }}
.tip {{ position: absolute; pointer-events: none; background: var(--card); color: var(--fg);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; font: 12px var(--sans);
  white-space: nowrap; box-shadow: 0 2px 8px rgb(0 0 0 / .12); transform: translate(-50%, -110%); }}
.panels {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; }}
table {{ width: 100%; border-collapse: collapse; font-family: var(--sans); font-size: .85rem; }}
td {{ padding: 3px 0; vertical-align: middle; }}
td.name {{ max-width: 0; width: 100%; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; padding-right: 8px; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; padding-right: 8px; white-space: nowrap; }}
td.meter {{ width: 72px; min-width: 72px; }}
td.meter span {{ display: block; height: 6px; border-radius: 3px; background: var(--meter); min-width: 2px; }}
.more {{ font-family: var(--sans); font-size: .78rem; margin: 6px 0 0; }}
.note {{ font-size: .92rem; max-width: 70ch; }}
.note li {{ margin-bottom: 4px; }}
</style>
</head>
<body>
<main>
<h1>Visitor stats</h1>
<p class="sub">math.tejstead.com &middot; counting since {since} &middot; updated {updated}
&middot; <a href="stats.json">stats.json</a></p>

<div class="tiles">{tiles}</div>

<h2>Last 48 hours</h2>
<div class="chart-wrap">{hour_chart}</div>

<h2>Last {n_days} days</h2>
<div class="chart-wrap">{day_chart}</div>

<h2>Last 30 days</h2>
<div class="panels">{panels}</div>

<h2>How this is counted</h2>
<ul class="note">
<li>Nothing runs in your browser: no cookies, no tracking script, no third-party
services. These numbers come from the web server's own access log, summarized
every ten minutes.</li>
<li>A page view is a browser loading a page. Link-preview fetchers, feed readers,
crawlers and scripts are left out ({bots30} such requests in the last 30 days).</li>
<li>A visitor is counted once per day using a hash of the IP address and browser
string with a random salt. The salt and hashes are deleted when the UTC day ends,
so nobody can be followed from one day to the next, and the stored history has no
IP addresses or hashes in it. Because of that, daily visitors can't be added up
into unique visitors for a week or a month; the longer totals are sums of daily
counts.</li>
<li>Referrers are recorded as the domain only. Browsers usually send only the
domain anyway.</li>
<li>Visitors on IPv6 currently reach the server through a shared internal address,
so the visitor count runs low; page views are unaffected.</li>
</ul>
</main>
<script>
for (const wrap of document.querySelectorAll(".chart-wrap")) {{
  const tip = document.createElement("div");
  tip.className = "tip"; tip.hidden = true; wrap.append(tip);
  wrap.addEventListener("pointermove", ev => {{
    const t = ev.target.closest(".hit");
    if (!t) {{ tip.hidden = true; return; }}
    const r = wrap.getBoundingClientRect(), b = t.getBoundingClientRect();
    tip.textContent = t.dataset.tip; tip.hidden = false;
    tip.style.left = Math.min(Math.max(b.left + b.width / 2 - r.left, 80), r.width - 80) + "px";
    tip.style.top = (ev.clientY - r.top) + "px";
  }});
  wrap.addEventListener("pointerleave", () => {{ tip.hidden = true; }});
  for (const t of wrap.querySelectorAll(".hit title")) t.remove();
}}
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--logs", nargs="+", help="read these log files instead of the container's")
    ap.add_argument("--state", type=Path, default=STATE)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    state = (json.loads(args.state.read_text()) if args.state.exists()
             else {"hwm": 0, "hours": {}, "days": {}, "today": None})
    sources = file_logs(args.logs) if args.logs else caddy_logs(state["hwm"])
    events = hits(sources, state["hwm"])
    tally(state, events)
    if events:
        state["hwm"] = events[-1].ts
        state.setdefault("since", events[0].ts)
    write_atomic(args.state, json.dumps(state, separators=(",", ":")), mode=0o600)
    write_atomic(args.out / "stats.json", json.dumps(public(state), separators=(",", ":")))
    write_atomic(args.out / "index.html", render(state))


if __name__ == "__main__":
    sys.exit(main())
