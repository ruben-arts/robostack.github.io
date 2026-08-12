"""Fetch the deployed package tables when the local copies are missing or stale.

`public/data/<distro>.json` is not stored in git for any distro that still has a
data channel; the deploy workflow regenerates those tables from the channels with
scripts/compare_pkg_completeness.py every six hours. Everywhere else - local
development and the checks workflow - this script downloads the currently
deployed copy from the live site instead, skipping any file younger than
`--max-age-hours`. Distros without a `dataChannel` in src/data/distros.json
(foxy, galactic) are committed snapshots and are left alone.

A stale copy that fails to redownload is kept with a warning; a distro that ends
up with no file at all is an error, because the site cannot build without it.

The download counts (`downloads-<distro>.json`, written by scripts/deb_downloads.py
in the deploy workflow) ride along under the same freshness rule, for every distro
rather than only the regenerable ones. Unlike the tables they are optional: the
page treats a missing file as "no popularity data", so failing to fetch one is
never an error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import niquests
from urllib3.util.retry import Retry

DISTROS_JSON = Path(__file__).parent.parent / "src" / "data" / "distros.json"
DATA_DIR = Path(__file__).parent.parent / "public" / "data"
SITE = "https://robostack.github.io/data"

session = niquests.Session(
    retries=Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
)


def main() -> None:
    """Download every regenerable distro's table that is missing or stale."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=6.0,
        help="redownload a table once the local copy is older than this (default: 6)",
    )
    args = parser.parse_args()
    max_age_seconds = args.max_age_hours * 3600

    missing: list[str] = []
    for entry in json.loads(DISTROS_JSON.read_text())["distros"]:
        name = entry["name"]
        # The table, for distros the pipeline regenerates. Required: without
        # it the page has nothing to show.
        if entry["dataChannel"]:
            ensure(f"{name}.json", max_age_seconds, missing)
        # The download counts, for every distro. Optional by design.
        ensure(f"downloads-{name}.json", max_age_seconds, None)

    if missing:
        sys.exit(f"no table data for: {', '.join(missing)}")


def ensure(filename: str, max_age_seconds: float, missing: list[str] | None) -> None:
    """Fetch one deployed data file unless the local copy is fresh.

    `missing` collects fatal absences; None marks the file as optional, where
    the only trace of a failed download is the warning.
    """
    path = DATA_DIR / filename
    if path.exists() and time.time() - path.stat().st_mtime < max_age_seconds:
        print(f"{filename}: fresh", file=sys.stderr)
        return
    try:
        response = session.get(f"{SITE}/{filename}")
        response.raise_for_status()
        body = response.content or b""
    except Exception as error:  # noqa: BLE001 - stale data is usable, absent data is not
        if path.exists():
            print(f"{filename}: keeping stale copy, download failed ({error})", file=sys.stderr)
        elif missing is None:
            print(f"{filename}: not available ({error})", file=sys.stderr)
        else:
            print(f"{filename}: no local copy and download failed ({error})", file=sys.stderr)
            missing.append(filename)
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    print(f"{filename}: downloaded", file=sys.stderr)


if __name__ == "__main__":
    main()
