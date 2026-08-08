"""
scripts/akinator/harvest_series_labels.py — names for the series ids.

    python scripts/akinator/harvest_series_labels.py

`harvest_works.py` records which Wikidata series each book belongs to, as a
QID. Grouping needs only the id, but **guessing needs the name** — the point
of pooling a series is to say "Harry Potter" out loud instead of naming the
wrong volume, and "Q8337" is not something to show a player.

Deliberately a separate pass rather than another OPTIONAL in the works
query. That query already returns a row per (work x adaptation x series)
combination and is the one that has to be batched down to 60 authors to
avoid the WDQS timeout; adding a label service to it makes the heavy query
heavier to save a cheap one. There are only a few hundred distinct series
ids and they fetch in two or three requests.

Output: data/akinator_series_labels.json (gitignored, regenerable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKS_PATH = os.path.join(REPO_ROOT, "data", "akinator_works_wd.json")
OUT_PATH = os.path.join(REPO_ROOT, "data", "akinator_series_labels.json")

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "Litheca/1.0 (https://litheca.com; hello@litheca.com)",
    "Accept": "application/sparql-results+json",
}
BATCH = 200

QUERY = """SELECT ?s ?sLabel WHERE {
  VALUES ?s { %s }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}"""


def series_ids(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        works = json.load(fh)
    return sorted({r["sid"] for r in works.values() if r.get("sid")})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--works", default=WORKS_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    ids = series_ids(args.works)
    labels: dict[str, str] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            labels = json.load(fh)

    todo = [i for i in ids if i not in labels]
    print(f"{len(ids)} series ids, {len(todo)} without a name yet.\n")

    failures = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        values = " ".join(f"wd:{q}" for q in chunk)
        url = ENDPOINT + "?" + urllib.parse.urlencode(
            {"query": QUERY % values, "format": "json"})
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                rows = json.load(resp)["results"]["bindings"]
            failures = 0
        except Exception as exc:  # noqa: BLE001
            # Skip the batch, do not abandon the run. The first version
            # broke out of the loop on any failure, so one flaky request
            # left 170 series unnamed and the Harry Potter grouping silently
            # switched off — a partial harvest that reported success.
            failures += 1
            print(f"  ! batch at {i} failed ({str(exc)[:60]}); skipping",
                  file=sys.stderr)
            if failures >= 4:
                print("  ! four in a row; stopping. Re-run to retry.",
                      file=sys.stderr)
                break
            time.sleep(5 * failures)
            continue

        for b in rows:
            qid = b["s"]["value"].rsplit("/", 1)[-1]
            label = b.get("sLabel", {}).get("value") or ""
            # An unlabelled item comes back as its own QID; that is not a
            # name a player would recognise, so treat it as no name at all.
            if label and label != qid:
                labels[qid] = label

        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(labels, fh, ensure_ascii=False)
        print(f"  {min(i + BATCH, len(todo)):>5}/{len(todo)}   "
              f"{len(labels)} named")
        time.sleep(1.5)

    print(f"\n{len(labels)} series named -> {args.out}")
    for qid, name in list(labels.items())[:8]:
        print(f"    {qid:<12} {name}")


if __name__ == "__main__":
    main()
