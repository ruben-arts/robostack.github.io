"""Build the .deb download counts behind the package pages' popularity data.

Writes `public/data/downloads-<distro>.json` for every distro in
src/data/distros.json. `src/components/package-table/PackageTable.svelte`
fetches it next to the package table and joins the two by package name, which
is what powers the download-weighted coverage figure, the "Most downloaded"
sort and the popularity marks. A missing file simply switches those features
off, so the EOL snapshots and a first deploy need nothing special.

The counts come from the monthly awstats reports for packages.ros.org
(https://awstats.osuosl.org/reports/packages.ros.org/), the access-log
statistics of the ROS apt repository: one hit is one .deb fetched by apt.
Hits include CI farms and mirrors, so the values are a popularity ranking,
not a user count. Each report covers one complete month and never changes
afterwards, which makes them cacheable forever; the deploy workflow keeps
`--cache-dir` in actions/cache so OSU serves each ~20 MB report only once.

Counts are per ROS distro (the deb filename carries it), so every page ranks
against the downloads of its own distro and a package released only for
humble does not haunt jazzy's ranking. Debian's -dbgsym debug-symbol
companions are excluded: they are build artifacts, not ROS packages.

Usage: python scripts/deb_downloads.py [--months 6] [--cache-dir DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import niquests
from urllib3.util.retry import Retry

DISTROS_JSON = Path(__file__).parent.parent / "src" / "data" / "distros.json"
DATA_DIR = Path(__file__).parent.parent / "public" / "data"

AWSTATS_BASE = "https://awstats.osuosl.org/reports/packages.ros.org"
# Older months predate the dedicated "downloads" page and only have
# "urldetail", which lists the same URL-and-hits table.
AWSTATS_PAGES = ("downloads", "urldetail")

# deb filename:  ros-<rosdistro>-<package>_<version>_<arch>.deb
DEB_RE = re.compile(r"^ros-([a-z0-9]+)-([^_/]+)_")
# One awstats row: the link to the file, then the cell holding its hit count.
ROW_RE = re.compile(
    r'href="https?://packages\.ros\.org/([^"]+?\.deb)"[^>]*>[^<]*</a></td><td>([\d,]+)</td>'
)

session = niquests.Session(
    retries=Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
)


def months_back(count: int) -> list[tuple[int, int]]:
    """The `count` most recent complete months, oldest first.

    The running month is skipped: its report only covers the days so far,
    which would rank packages against a partial month.
    """
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
    """The awstats per-URL report for one month, from cache or from OSU.

    Returns None when that month has no report (nothing published yet, or a
    gap in the logs), which is a skip rather than an error.
    """
    cached = cache_dir / f"packages_{year}_{month:02d}.html"
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")

    for page in AWSTATS_PAGES:
        url = f"{AWSTATS_BASE}/{year}/{month:02d}/awstats.packages.ros.org.{page}.html"
        try:
            response = session.get(url, timeout=300)
        except Exception as error:  # noqa: BLE001 - a lost month is a skip, not a crash
            print(f"  {year}-{month:02d}: {error}", file=sys.stderr)
            return None
        if response.status_code == 404:
            continue
        if not response.ok:
            print(f"  {year}-{month:02d}: HTTP {response.status_code}", file=sys.stderr)
            return None
        body = response.text or ""
        # awstats serves a 200 with a 404 body for months it has no page for.
        if "404 Not Found" in body[:2000]:
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(body, encoding="utf-8")
        return body

    print(f"  {year}-{month:02d}: no report published, skipped", file=sys.stderr)
    return None


def count_downloads(html: str) -> dict[str, dict[str, int]]:
    """ROS distro -> base package name -> .deb hits in one monthly report."""
    per_distro: dict[str, dict[str, int]] = {}
    for path, hits in ROW_RE.findall(html):
        filename = path.rsplit("/", 1)[-1]
        match = DEB_RE.match(filename)
        if not match:
            continue
        distro, package = match.group(1), match.group(2)
        name = package.strip().lower()
        # Debian debug-symbol companions, not ROS packages.
        if name.endswith("-dbgsym"):
            continue
        counts = per_distro.setdefault(distro, {})
        counts[name] = counts.get(name, 0) + int(hits.replace(",", ""))
    return per_distro


def main() -> int:
    """Count the last N complete months and write one JSON per distro."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="complete months of download data (default: %(default)s)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "ros-awstats-cache",
        help="where the monthly reports are cached (default: %(default)s)",
    )
    parser.add_argument(
        "--prune-cache",
        action="store_true",
        help="delete cached reports for months outside the current window; the deploy "
        "workflow passes this so the actions/cache entry stays at N months forever",
    )
    args = parser.parse_args()
    if args.months < 1:
        print("--months must be at least 1", file=sys.stderr)
        return 2

    months = months_back(args.months)
    if args.prune_cache and args.cache_dir.is_dir():
        keep = {f"packages_{year}_{month:02d}.html" for year, month in months}
        for stale in sorted(args.cache_dir.glob("packages_*.html")):
            if stale.name not in keep:
                stale.unlink()
                print(f"pruned {stale.name}", file=sys.stderr)
    print(f"reading {len(months)} monthly awstats reports (cache: {args.cache_dir}) ...")
    totals: dict[str, dict[str, int]] = {}
    got = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = pool.map(lambda ym: (ym, fetch_report(ym[0], ym[1], args.cache_dir)), months)
        for (year, month), html in reports:
            if html is None:
                continue
            got += 1
            for distro, counts in count_downloads(html).items():
                merged = totals.setdefault(distro, {})
                for name, hits in counts.items():
                    merged[name] = merged.get(name, 0) + hits
            print(f"  {year}-{month:02d}: ok")
    if not got:
        print("no download data collected; nothing to write", file=sys.stderr)
        return 1

    window = f"{months[0][0]}-{months[0][1]:02d} to {months[-1][0]}-{months[-1][1]:02d}"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for entry in json.loads(DISTROS_JSON.read_text())["distros"]:
        name = entry["name"]
        counts = totals.get(name, {})
        # Sorted by hits so ranks are simply the object's insertion order,
        # which JSON preserves and the page relies on.
        doc = {
            "distro": name,
            "window": window,
            "source": f"{AWSTATS_BASE}/",
            "packages": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        }
        path = DATA_DIR / f"downloads-{name}.json"
        path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
        print(f"{name}: {len(counts)} packages, {sum(counts.values()):,} hits -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
