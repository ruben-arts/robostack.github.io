#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build a local HTML report of ROS package popularity vs RoboStack coverage.

The terminal companion (compare-deb-vs-robostack.py) prints the coverage
buckets and the top missing packages; this script renders the same data as a
single self-contained HTML page: download-weighted coverage, coverage of the
top 100/250/500/... most-downloaded packages, the most-wanted missing list,
and a searchable table of every ranked package.

Each report ranks against the .deb downloads of its own distro, so the
denominator only contains packages actually released for it; --aggregate
ranks against the downloads summed across every ROS 2 distro instead (the
companion script's behaviour, useful for one comparable list).

Local use only. The page is written next to this script (deb-coverage-report.html,
gitignored) and is not part of the site build or the deploy workflow.

Data sources and caching are identical to compare-deb-vs-robostack.py: the OSU
awstats reports for packages.ros.org (one ~20 MB HTML report per month, cached
in --cache-dir, shared with the other script) and repodata.json per subdir from
prefix.dev.

Run:
    pixi76 run --script deb-coverage-report.py
    pixi76 run --script deb-coverage-report.py -- --months 12 --distro jazzy --open
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import gettempdir

# --------------------------------------------------------------------------- #
# config (kept in sync with compare-deb-vs-robostack.py)
# --------------------------------------------------------------------------- #
AWSTATS_URL = "https://awstats.osuosl.org/reports/packages.ros.org/{year}/{month:02d}/"
AWSTATS_PAGES = ("downloads", "urldetail")

DEFAULT_DISTRO = "lyrical"
DEFAULT_CHANNEL = "https://prefix.dev/robostack-{distro}"
SUBDIRS = ("noarch", "linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")

# What --all iterates over: the ROS 2 distros that still have a live channel
# (the non-EOL entries of src/data/distros.json), oldest first.
ALL_DISTROS = ("humble", "jazzy", "kilted", "rolling", "lyrical")

ROS2_DISTROS = frozenset(
    {"foxy", "galactic", "humble", "iron", "jazzy", "kilted", "rolling", "lyrical"}
)

DEB_RE = re.compile(r"^ros-([a-z0-9]+)-([^_/]+)_")
ROW_RE = re.compile(
    r'href="https?://packages\.ros\.org/([^"]+?\.deb)"[^>]*>[^<]*</a></td><td>([\d,]+)</td>'
)


# --------------------------------------------------------------------------- #
# data collection
# --------------------------------------------------------------------------- #
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "deb-coverage-report/1.0"})
    with urllib.request.urlopen(req, timeout=300) as response:
        return response.read()


def months_back(count: int) -> list[tuple[int, int]]:
    """The `count` most recent complete months, oldest first."""
    today = dt.date.today()
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        months.append((year, month))
    return sorted(months)


def fetch_report(year: int, month: int, cache_dir: Path) -> str | None:
    """The awstats per-URL report for one month, from cache or from OSU."""
    cached = cache_dir / f"packages_{year}_{month:02d}.html"
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")

    base = AWSTATS_URL.format(year=year, month=month)
    for page in AWSTATS_PAGES:
        url = f"{base}awstats.packages.ros.org.{page}.html"
        try:
            body = http_get(url).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            print(f"  {year}-{month:02d}: {error}", file=sys.stderr)
            return None
        except OSError as error:
            print(f"  {year}-{month:02d}: {error}", file=sys.stderr)
            return None
        if "404 Not Found" in body[:2000]:
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(body, encoding="utf-8")
        return body

    print(f"  {year}-{month:02d}: no report published, skipped", file=sys.stderr)
    return None


def count_downloads(html: str) -> dict[str, dict[str, int]]:
    """ROS distro -> base package name -> .deb hits in one monthly report.

    Split per distro rather than pre-merged: each report ranks against the
    downloads of its own distro by default, and only --aggregate folds the
    distros together, so the split has to survive until then.
    """
    per_distro: dict[str, dict[str, int]] = {}
    for path, hits in ROW_RE.findall(html):
        filename = path.rsplit("/", 1)[-1]
        match = DEB_RE.match(filename)
        if not match:
            continue
        distro, package = match.group(1), match.group(2)
        name = package.strip().lower()
        # Debian debug-symbol companions, not ROS packages; they would only
        # pad the ranking and the missing list with entries conda never has.
        if name.endswith("-dbgsym"):
            continue
        counts = per_distro.setdefault(distro, {})
        counts[name] = counts.get(name, 0) + int(hits.replace(",", ""))
    return per_distro


def get_ros_downloads(months: list[tuple[int, int]], cache_dir: Path) -> dict[str, dict[str, int]]:
    """ROS distro -> base package name -> total .deb downloads over `months`."""
    print(f"reading {len(months)} monthly awstats reports (cache: {cache_dir}) ...")
    totals: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = pool.map(lambda ym: (ym, fetch_report(ym[0], ym[1], cache_dir)), months)
        for (year, month), html in reports:
            if html is None:
                continue
            counted = count_downloads(html)
            hits_month = 0
            for distro, counts in counted.items():
                merged = totals.setdefault(distro, {})
                for name, hits in counts.items():
                    merged[name] = merged.get(name, 0) + hits
                    hits_month += hits
            print(f"  {year}-{month:02d}: {len(counted)} distros, {hits_month:,} hits")
    for distro in sorted(totals):
        print(f"  {distro}: {len(totals[distro])} packages with downloads")
    return totals


def merge_downloads(
    per_distro: dict[str, dict[str, int]], distros: frozenset[str] | None
) -> dict[str, int]:
    """One ranking across `distros` (all of them when None), for --aggregate."""
    merged: dict[str, int] = {}
    for distro, counts in per_distro.items():
        if distros is not None and distro not in distros:
            continue
        for name, hits in counts.items():
            merged[name] = merged.get(name, 0) + hits
    return merged


def get_channel_platforms(channel: str, distro: str) -> dict[str, int]:
    """base package name -> bitmask over SUBDIRS where a build exists.

    Richer than the yes/no set the terminal script uses: the report shows how
    many platforms carry each package, so partial builds are visible too.
    """
    prefix = f"ros-{distro}-"
    masks: dict[str, int] = {}
    for bit, subdir in enumerate(SUBDIRS):
        url = f"{channel.rstrip('/')}/{subdir}/repodata.json"
        try:
            data = json.loads(http_get(url))
        except (OSError, ValueError) as error:
            print(f"  skip {subdir}: {error}", file=sys.stderr)
            continue
        names = {
            meta["name"]
            for section in ("packages", "packages.conda")
            for meta in data.get(section, {}).values()
        }
        count = 0
        for name in names:
            if not name.startswith(prefix):
                continue
            base = name[len(prefix) :].lower()
            masks[base] = masks.get(base, 0) | (1 << bit)
            count += 1
        print(f"  {subdir}: {count} {prefix}* packages")
    print(f"  {len(masks)} distinct {prefix}* packages on the channel")
    return masks


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
# Everything numeric is computed in the page's script from the embedded rows,
# so the Python side only gathers data. Self-contained: no external assets,
# works from file://.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b6b6b; --faint: #a3a3a3;
    --line: #e2e2e2; --card: #f7f7f5;
    --accent: #1a6fc4; --accent-soft: #d8e8f8;
    --good: #3f7a12; --good-bg: #e7f4d6;
    --miss: #8a8a8a; --miss-bg: #ececec;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181c; --fg: #e8e8e8; --muted: #9a9a9a; --faint: #6b6b6b;
      --line: #33353b; --card: #1f2126;
      --accent: #5ea3e8; --accent-soft: #1d3a5c;
      --good: #9ad668; --good-bg: #22380f;
      --miss: #9a9a9a; --miss-bg: #2a2c32;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0 auto; padding: 2rem 1.25rem 4rem; max-width: 62rem;
    background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  h1 { font-size: 1.4rem; font-weight: 600; margin: 0 0 0.25rem; }
  .sub { color: var(--muted); font-size: 0.85rem; margin: 0 0 1.5rem; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
           gap: 0.75rem; margin-bottom: 1.5rem; }
  .tile { background: var(--card); border-radius: 0.5rem; padding: 0.9rem 1.1rem; }
  .tile b { display: block; font-size: 1.7rem; font-weight: 600;
            font-variant-numeric: tabular-nums; }
  .tile span { font-size: 0.8rem; color: var(--muted); }
  /* The download-weighted figure is the page's answer; the other tiles are
     context. */
  .tile.hero { border: 2px solid var(--accent); padding: calc(0.9rem - 2px) calc(1.1rem - 2px); }
  .tile.hero b { color: var(--accent); }
  .explain { color: var(--muted); font-size: 0.85rem; margin: 0 0 1.5rem; max-width: 46rem; }
  .explain b { color: var(--fg); }

  section { margin-bottom: 2rem; }
  h2 { font-size: 1.05rem; font-weight: 600; margin: 0 0 0.2rem; }
  .hint { color: var(--muted); font-size: 0.8rem; margin: 0 0 0.75rem; }

  .buckets { display: grid; grid-template-columns: auto 1fr auto auto;
             gap: 0.45rem 0.9rem; align-items: center; font-size: 0.85rem; }
  .buckets .head { color: var(--faint); font-size: 0.75rem; }
  .buckets .lbl { color: var(--muted); white-space: nowrap; }
  /* Grid items are blockified automatically; the fill inside the track is
     not, and an inline span ignores its width. */
  .track { height: 10px; background: var(--card); border-radius: 999px; overflow: hidden; }
  .fill { display: block; height: 100%; background: var(--accent);
          border-radius: 999px 0 0 999px; }
  .val { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
  .val small { color: var(--muted); }

  .tools { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin: 0 0 0.75rem; }
  .tools input, .tools select {
    font: inherit; font-size: 0.85rem; color: var(--fg); background: var(--bg);
    border: 1px solid var(--line); border-radius: 0.3rem; padding: 0.4em 0.7em;
  }
  .tools input { flex: 1 1 14rem; min-width: 0; }
  .chip {
    font: inherit; font-size: 0.8rem; cursor: pointer; white-space: nowrap;
    border: 1px solid var(--line); border-radius: 0.3rem; padding: 0.35em 0.8em;
    background: var(--bg); color: var(--fg);
  }
  .chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .count { color: var(--muted); font-size: 0.8rem; margin: 0 0 0.5rem; }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 0.75rem;
       padding: 0.4em 0.6em; border-bottom: 1px solid var(--line); white-space: nowrap; }
  td { padding: 0.45em 0.6em; border-bottom: 1px solid var(--line); vertical-align: middle; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .rank { color: var(--faint); }
  .pkg a { color: inherit; text-decoration: none; }
  .pkg a:hover { color: var(--accent); text-decoration: underline; }
  .prefix { color: var(--faint); }
  .share { color: var(--muted); font-size: 0.9em; }
  .dlbar { display: inline-block; width: 4.5rem; height: 7px; background: var(--card);
           border-radius: 999px; overflow: hidden; vertical-align: middle; margin-left: 0.6rem; }
  .dlbar i { display: block; height: 100%; background: var(--accent-soft);
             border-right: 2px solid var(--accent); }
  .pill { display: inline-block; border-radius: 999px; padding: 0.1em 0.7em;
          font-size: 0.78rem; white-space: nowrap; }
  .pill.yes { background: var(--good-bg); color: var(--good); }
  .pill.no { background: var(--miss-bg); color: var(--miss); }
  .foot { color: var(--faint); font-size: 0.75rem; margin-top: 2rem; }
  .sub a, .foot a { color: inherit; }
  .sub a:hover, .foot a:hover { color: var(--accent); }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="sub">__SUBTITLE__</p>

<div class="tiles" id="tiles"></div>
<p class="explain" id="explain"></p>

<section>
  <h2>Coverage of the most-downloaded packages</h2>
  <p class="hint">Per bucket of the download ranking: how many packages are on the
  channel, and how much of the bucket's downloads they represent.</p>
  <div class="buckets" id="buckets"></div>
</section>

<section>
  <h2>Most-wanted missing packages</h2>
  <p class="hint">Highest-ranked packages with no build on the channel &mdash; the
  ordering to add packages in for the most impact.</p>
  <table>
    <thead><tr><th class="num">#</th><th>Package</th><th class="num">Downloads</th>
    <th class="num">Share</th></tr></thead>
    <tbody id="wanted"></tbody>
  </table>
</section>

<section>
  <h2>All ranked packages</h2>
  <div class="tools">
    <input type="search" id="q" placeholder="Search packages&hellip;"
           autocomplete="off" spellcheck="false">
    <span id="chips"></span>
    <select id="limit">
      <option value="100">Top 100</option>
      <option value="500">Top 500</option>
      <option value="1000" selected>Top 1000</option>
      <option value="2000">Top 2000</option>
      <option value="0">All</option>
    </select>
  </div>
  <p class="count" id="showing"></p>
  <table>
    <thead><tr><th class="num">#</th><th>Package</th><th class="num">Downloads</th>
    <th>On channel</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</section>

<p class="foot">Downloads are .deb hits from the monthly
<a href="https://awstats.osuosl.org/reports/packages.ros.org/" target="_blank"
rel="noreferrer">awstats reports for packages.ros.org</a> (OSU mirror), which
include CI farms and mirrors &mdash; treat them as a popularity ranking, not a
user count. Channel availability comes from
<a href="__CHANNEL_URL__" target="_blank" rel="noreferrer">__CHANNEL_URL__</a>
(repodata.json per platform). Generated locally by deb-coverage-report.py; not
part of the deployed site.</p>

<script>
const D = __PAYLOAD__;
const rows = D.rows; // [rank, name, downloads, platformMask]
const nf = new Intl.NumberFormat("en-US");

const totalDl = rows.reduce((n, r) => n + r[2], 0);
const availRows = rows.filter(r => r[3] !== 0);
const availDl = availRows.reduce((n, r) => n + r[2], 0);
const maxDl = rows.length ? rows[0][2] : 1;

function pct(x, digits = 1) { return (100 * x).toFixed(digits) + "%"; }
function bits(mask) { let n = 0; while (mask) { n += mask & 1; mask >>= 1; } return n; }
function platforms(mask) {
  return D.subdirs.filter((_, i) => (mask >> i) & 1).join(", ");
}
function fmtDl(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  return nf.format(n);
}

/* ---- tiles ---- */
document.getElementById("tiles").innerHTML = [
  [pct(availDl / totalDl), "of all downloads covered (download-weighted)", "hero"],
  [pct(availRows.length / rows.length, 0), availRows.length.toLocaleString() +
    " of " + rows.length.toLocaleString() + " ranked packages on the channel", ""],
  [fmtDl(totalDl), "downloads counted over " + D.window, ""],
  [D.channelOnly.toLocaleString(), "channel packages with no ranked downloads", ""],
].map(([b, s, cls]) =>
  '<div class="tile ' + cls + '"><b>' + b + "</b><span>" + s + "</span></div>").join("");

/* The one computed sentence on the page: what the hero number means, spelled
 * out with this report's own figures. */
document.getElementById("explain").innerHTML =
  "<b>Downloads covered</b> weighs every package by how often its .deb was " +
  "fetched from packages.ros.org: the " + availRows.length.toLocaleString() +
  " packages on the channel are only " + pct(availRows.length / rows.length, 0) +
  " of the " + rows.length.toLocaleString() + " ranked packages, but they " +
  "account for " + pct(availDl / totalDl) + " of the " + fmtDl(totalDl) +
  " downloads in this window. A plain package count values a rarely-installed " +
  "demo package the same as <code>rclcpp</code>; this number answers how much " +
  "of real-world ROS usage the channel already covers.";

/* ---- buckets ---- */
const buckets = [100, 250, 500, 1000, 2000].filter(n => n < rows.length);
buckets.push(rows.length);
document.getElementById("buckets").innerHTML =
  '<span></span><span></span><span class="head val">packages</span>' +
  '<span class="head val">downloads</span>' +
  buckets.map(n => {
    const chunk = rows.slice(0, n);
    const have = chunk.filter(r => r[3] !== 0);
    const dl = chunk.reduce((s, r) => s + r[2], 0);
    const haveDl = have.reduce((s, r) => s + r[2], 0);
    const label = n === rows.length ? "All " + n.toLocaleString() : "Top " + n;
    return '<span class="lbl">' + label + "</span>" +
      '<span class="track"><span class="fill" style="width:' +
      pct(have.length / n) + '"></span></span>' +
      '<span class="val">' + pct(have.length / n, 0) +
      " <small>(" + have.length + "/" + n + ")</small></span>" +
      '<span class="val">' + pct(haveDl / dl) + "</span>";
  }).join("");

/* ---- most wanted ---- */
document.getElementById("wanted").innerHTML = rows
  .filter(r => r[3] === 0).slice(0, D.topMissing)
  .map(r => "<tr><td class='num rank'>" + r[0] + "</td><td class='pkg'>" + nameCell(r[1]) +
    "</td><td class='num'>" + fmtDl(r[2]) + "</td><td class='num share'>" +
    pct(r[2] / totalDl, 2) + "</td></tr>").join("");

function nameCell(name) {
  const ros = "https://index.ros.org/p/" + encodeURIComponent(name.replace(/-/g, "_")) + "/";
  return '<span class="prefix">ros-' + D.distro + '-</span><a href="' + ros +
    '" target="_blank" rel="noreferrer"><code>' + name + "</code></a>";
}

/* ---- full table ---- */
const FILTERS = [
  ["all", "All", () => true],
  ["available", "Available", r => r[3] !== 0],
  ["missing", "Missing", r => r[3] === 0],
];
let filter = "all";
const chipsEl = document.getElementById("chips");
chipsEl.innerHTML = FILTERS.map(([id, label, test]) =>
  '<button class="chip" data-f="' + id + '">' + label + " " +
  rows.filter(test).length.toLocaleString() + "</button>").join(" ");
chipsEl.addEventListener("click", e => {
  const b = e.target.closest("[data-f]");
  if (!b) return;
  filter = b.dataset.f;
  render();
});

const qEl = document.getElementById("q");
const limitEl = document.getElementById("limit");
qEl.addEventListener("input", render);
limitEl.addEventListener("change", render);

function render() {
  for (const b of chipsEl.querySelectorAll(".chip")) {
    b.classList.toggle("on", b.dataset.f === filter);
  }
  const q = qEl.value.trim().toLowerCase();
  const test = FILTERS.find(f => f[0] === filter)[2];
  const matched = rows.filter(r => test(r) && (!q || r[1].includes(q)));
  const limit = parseInt(limitEl.value, 10);
  const shown = limit ? matched.slice(0, limit) : matched;
  document.getElementById("showing").textContent =
    "Showing " + shown.length.toLocaleString() + " of " +
    matched.length.toLocaleString() + " matching packages.";
  document.getElementById("rows").innerHTML = shown.map(r => {
    const n = bits(r[3]);
    const avail = r[3] !== 0
      ? '<span class="pill yes" title="' + platforms(r[3]) + '">' +
        n + "/" + D.subdirs.length + " platforms</span>"
      : '<span class="pill no">missing</span>';
    // log scale: linear would flatten everything below the top ten into
    // invisibility, the ranking spans four orders of magnitude.
    const w = Math.max(2, 100 * Math.log(r[2] + 1) / Math.log(maxDl + 1));
    return "<tr><td class='num rank'>" + r[0] + "</td><td class='pkg'>" + nameCell(r[1]) +
      "</td><td class='num'>" + fmtDl(r[2]) +
      '<span class="dlbar"><i style="width:' + w.toFixed(0) + '%"></i></span>' +
      "</td><td>" + avail + "</td></tr>";
  }).join("");
}
render();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--distro", default=DEFAULT_DISTRO, help="RoboStack distro to check (default: %(default)s)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"write one report per live ROS 2 channel ({', '.join(ALL_DISTROS)}); "
        "ignores --distro, --channel and --output",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="channel base URL (default: https://prefix.dev/robostack-<distro>)",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="complete months of download data (default: %(default)s)",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="rank against downloads summed across every ROS 2 distro instead of "
        "only the report's own distro (the old behaviour)",
    )
    parser.add_argument(
        "--all-distros",
        action="store_true",
        help="with --aggregate: include ROS 1 downloads too, not just ROS 2",
    )
    parser.add_argument(
        "--top-missing",
        type=int,
        default=25,
        help="rows in the most-wanted missing table (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="where to write the report (default: deb-coverage-report-<distro>.html)",
    )
    parser.add_argument("--open", action="store_true", help="open the report in a browser")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(gettempdir()) / "ros-awstats-cache",
        help="where the monthly reports are cached (default: %(default)s)",
    )
    return parser.parse_args()


def write_report(
    distro: str,
    channel: str,
    downloads: dict[str, int],
    scope: str,
    window: str,
    output: Path,
    args: argparse.Namespace,
) -> None:
    print(f"reading {channel} ...")
    masks = get_channel_platforms(channel, distro)

    ranked = sorted(downloads.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = [[i + 1, name, hits, masks.get(name, 0)] for i, (name, hits) in enumerate(ranked)]

    payload = {
        "distro": distro,
        "subdirs": SUBDIRS,
        "window": window,
        "topMissing": args.top_missing,
        # On the channel but absent from the ranking: never downloaded as a
        # .deb in the window (or has no deb release at all).
        "channelOnly": sum(1 for name in masks if name not in downloads),
        "rows": rows,
    }

    page = (
        PAGE.replace("__TITLE__", f"robostack-{distro} vs packages.ros.org downloads")
        .replace(
            "__SUBTITLE__",
            f'{scope} .deb downloads {window} &middot; channel <a href="{channel}" '
            f'target="_blank" rel="noreferrer">{channel.removeprefix("https://")}</a>'
            f" &middot; generated {dt.date.today().isoformat()}",
        )
        .replace("__CHANNEL_URL__", channel)
        # "</" would end the script block early if a value ever contained it.
        .replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")).replace("</", "<\\/"))
    )
    output.write_text(page, encoding="utf-8")
    print(f"wrote {output} ({len(rows)} packages)\n")

    if args.open:
        webbrowser.open(output.resolve().as_uri())


def main() -> int:
    args = parse_args()
    if args.months < 1:
        print("--months must be at least 1", file=sys.stderr)
        return 2

    months = months_back(args.months)
    # Fetched and parsed once, split per ROS distro; each report then picks
    # its own distro's counts (or the merged ranking with --aggregate).
    per_distro = get_ros_downloads(months, args.cache_dir)
    if not per_distro:
        print("no download data collected; nothing to report", file=sys.stderr)
        return 1
    window = f"{months[0][0]}-{months[0][1]:02d} to {months[-1][0]}-{months[-1][1]:02d}"

    def ranking(distro: str) -> tuple[dict[str, int], str]:
        """The downloads a report ranks against, plus the label naming them."""
        if not args.aggregate:
            return per_distro.get(distro, {}), distro
        if args.all_distros:
            return merge_downloads(per_distro, None), "ROS 1 + ROS 2"
        return merge_downloads(per_distro, ROS2_DISTROS), "ROS 2"

    here = Path(__file__).parent
    targets = (
        [
            (d, DEFAULT_CHANNEL.format(distro=d), here / f"deb-coverage-report-{d}.html")
            for d in ALL_DISTROS
        ]
        if args.all
        else [
            (
                args.distro,
                args.channel or DEFAULT_CHANNEL.format(distro=args.distro),
                args.output or here / f"deb-coverage-report-{args.distro}.html",
            )
        ]
    )
    for distro, channel, output in targets:
        downloads, scope = ranking(distro)
        if not downloads:
            print(f"{distro}: no .deb downloads recorded in this window, skipped", file=sys.stderr)
            continue
        write_report(distro, channel, downloads, scope, window, output, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
