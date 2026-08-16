#!/usr/bin/env python3
"""
Auto Collections coverage report.

Answers one question: which studios in the Jellyfin library are NOT yet
represented by any Auto Collections hub? New releases keep introducing studio
names, and a brand hub only stays useful if someone notices the gaps.

This deliberately does NOT create collections. Generating one collection per
studio produces hundreds of fragments and destroys the curated
OR-merges (Marvel Studios + Marvel Entertainment, Warner Bros + New Line +
Castle Rock, ...). Curation stays human; only the detection is automated.

Coverage mirrors the plugin's own matching: a hub covers a studio when any
quoted term in its expression is a SUBSTRING of the studio name, honouring
that hub's CaseSensitive flag. Getting this wrong in either direction produces
a useless report, so it tracks the plugin rather than guessing.

Reads JELLYFIN_URL / JELLYFIN_API_KEY from the environment, falling back to
the repo .env. Standard library only.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter

PLUGIN_ID = "06ebf4a9-1326-4327-968d-8da00e1ea2eb"  # Auto Collections
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Production/financing shells: high item counts, zero brand recognition. A
# viewer browsing the library never looks for "TSG Entertainment". Listed so
# the report stays a list of real candidates instead of noise to re-skim weekly.
IGNORE = {
    "tsg entertainment", "village roadshow", "di bonaventura", "dune entertainment",
    "temple hill", "relativity media", "pascal pictures", "ratpac", "lstar capital",
    "genre films", "mrc", "the donners' company", "1492 pictures", "arad productions",
    "sunswept entertainment", "original film", "silver pictures", "scott free",
}


def load_env():
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        if os.environ.get(key):
            continue
        path = os.path.join(REPO, ".env")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    os.environ[key] = line.split("=", 1)[1].strip()
    url = os.environ.get("JELLYFIN_URL")
    api = os.environ.get("JELLYFIN_API_KEY")
    if not url or not api:
        sys.exit("JELLYFIN_URL and JELLYFIN_API_KEY must be set (env or .env)")
    return url.rstrip("/"), api


def api(url, key, path, **params):
    full = f"{url}/{path}"
    if params:
        full += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"Authorization": f'MediaBrowser Token="{key}"'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_studios(url, key):
    """studio name -> item count, across Movies and Series."""
    counts = Counter()
    start, total = 0, None
    while True:
        data = api(url, key, "Items", Recursive="true", IncludeItemTypes="Movie,Series",
                   Fields="Studios", EnableImages="false", EnableTotalRecordCount="true",
                   StartIndex=start, Limit=500)
        items = data.get("Items", [])
        if total is None:
            total = data.get("TotalRecordCount", len(items))
        for item in items:
            for studio in item.get("Studios") or []:
                name = (studio.get("Name") or "").strip()
                if name:
                    counts[name] += 1
        start += len(items)
        if not items or start >= total:
            break
    return counts, total


def covered_by(hubs):
    """Return a predicate matching the plugin's substring + CaseSensitive rules."""
    terms = []  # (term, case_sensitive)
    for hub in hubs:
        cs = bool(hub.get("CaseSensitive"))
        for term in re.findall(r'"([^"]+)"', hub.get("Expression") or ""):
            terms.append((term, cs))

    def covered(studio):
        for term, cs in terms:
            if (term in studio) if cs else (term.lower() in studio.lower()):
                return True
        return False

    return covered


def main():
    url, key = load_env()
    threshold = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    cfg = api(url, key, f"Plugins/{PLUGIN_ID}/Configuration")
    hubs = cfg.get("ExpressionCollections") or []
    counts, total = fetch_studios(url, key)
    covered = covered_by(hubs)

    # Substring, not equality: the shells appear with varying suffixes
    # ("Village Roadshow" vs "Village Roadshow Pictures").
    def ignored(name):
        low = name.lower()
        return any(shell in low for shell in IGNORE)

    uncovered = sorted(
        ((n, c) for n, c in counts.items()
         if c >= threshold and not covered(n) and not ignored(n)),
        key=lambda x: -x[1],
    )

    print(f"Auto Collections coverage - {total} items, {len(counts)} studios, {len(hubs)} hubs")
    print(f"Uncovered studios with >= {threshold} items (financing shells filtered):\n")
    if not uncovered:
        print("  none - every studio above the threshold is already in a hub.")
        return
    print(f"  {'COUNT':>5}  STUDIO")
    for name, n in uncovered:
        print(f"  {n:>5}  {name}")
    print(f"\n  {len(uncovered)} candidate(s). Add the ones that are real brands as a hub in")
    print("  Dashboard > Plugins > Auto Collections, then re-run the Auto Collections task.")
    print("  Short/generic hub names (FOX, DC) get renamed by the TMDB scraper - use a")
    print("  distinctive name and lock the Name field on the resulting collection.")


if __name__ == "__main__":
    main()
