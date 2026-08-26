"""
scripts/akinator/sync_fandom_candidates.py — akinator_fandom.json -> candidates.json.

    python scripts/akinator/sync_fandom_candidates.py
    python scripts/akinator/sync_fandom_candidates.py --bookhub-dir ../bookhub

WHAT THIS CLOSES. discover_fandom.py and harvest_fandom.py prove wikis into
the local, gitignored data/akinator_fandom.json — Render never sees it, and
neither does the admin review panel. Getting a proven wiki in front of the
panel used to be a one-off manual copy-paste (see the 2026-08-26 vault note);
this script is the repeatable version of that copy-paste, run by hand after
a discovery session.

WHAT IT DOES NOT DO. It never touches FANDOM_WIKIS, wikis.json, or approves
anything — discover_fandom.py's docstring is explicit that proving a wiki
exists is not the same judgement as trusting it live, and that gate stays
human-only via the admin panel. This script only fills the queue that panel
reads from (candidates.json), and only with entries not already live or
already pending. It never commits or pushes; review and push stay manual.

KEYING. wikis.json and candidates.json are both keyed by subdomain (the
Fandom slug), not by title — a title can map to the wrong subdomain (see
"Against the Gods" in the vault note), so subdomain is the only safe
identity check here. Existing candidates are left byte-for-byte untouched:
an owner may have hand-edited aliases/author/cover_url from the review
panel, and re-deriving those fields would silently discard that edit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUND_PATH = os.path.join(REPO_ROOT, "data", "akinator_fandom.json")
DEFAULT_BOOKHUB_DIR = os.path.join(REPO_ROOT, "..", "bookhub")


def _load_json(path: str, what: str) -> dict:
    if not os.path.exists(path):
        print(f"error: {what} not found at {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bookhub-dir", default=DEFAULT_BOOKHUB_DIR,
                     help="path to the sibling bookhub repo checkout")
    args = ap.parse_args()

    candidates_path = os.path.join(args.bookhub_dir, "games", "data", "fandom", "candidates.json")
    wikis_path = os.path.join(args.bookhub_dir, "games", "data", "fandom", "wikis.json")

    found = _load_json(FOUND_PATH, "data/akinator_fandom.json")
    wikis = _load_json(wikis_path, "wikis.json")
    candidates = _load_json(candidates_path, "candidates.json")

    new_entries = {}
    skipped_malformed = []
    for title, info in found.items():
        sub = info.get("subdomain") if isinstance(info, dict) else None
        if not sub:
            skipped_malformed.append(title)
            continue
        if sub in wikis or sub in candidates:
            continue
        new_entries[sub] = {
            "aliases": [],
            "author": None,
            "cover_url": None,
            "seed_title": title,
            "subdomain": sub,
            "why": info.get("why", ""),
        }

    if skipped_malformed:
        print(f"warning: {len(skipped_malformed)} entries in akinator_fandom.json "
              f"have no 'subdomain' field, skipped: {skipped_malformed}", file=sys.stderr)

    if not new_entries:
        print("Nothing new - candidates.json already covers every proven wiki.")
        return

    candidates.update(new_entries)
    # candidates.json is committed with CRLF line endings and no trailing
    # newline (Windows checkout) — match that exactly so the diff is only
    # the new entries, not a whole-file line-ending rewrite.
    text = json.dumps(candidates, ensure_ascii=False, indent=1, sort_keys=True)
    with open(candidates_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", "\r\n"))

    print(f"Added {len(new_entries)} new candidate(s) to {candidates_path}:")
    for sub, entry in sorted(new_entries.items()):
        print(f"  - {sub}  ({entry['seed_title']})")
    print("\nNot committed - review in the admin Fandom tab, then push by hand.")


if __name__ == "__main__":
    main()
