"""
scripts/r2_usage_check.py — is R2 usage approaching the free tier?

    python scripts/r2_usage_check.py --threshold 90

Exit 0 when every dimension is below the threshold, 1 when any is over OR
when any could not be READ. That second case matters: a monitor that reports
green while blind turns an unknown into a reassurance, which is worse than no
monitor at all.

Reads:
  * storage — GET /accounts/{id}/r2/metrics   (needs Workers R2 Storage: Read)
  * operations — GraphQL r2OperationsAdaptiveGroups, current calendar month
    (needs Account Analytics: Read)

Run by .github/workflows/r2-usage-alert.yml every Monday. Written as a script
rather than inline shell so the classification below can be tested — the first
draft was jq embedded in YAML, which cannot be run anywhere except a runner.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

API = "https://api.cloudflare.com/client/v4"

# Free tier, from Cloudflare's pricing page.
LIMIT_BYTES = 10 * 1000 * 1000 * 1000   # 10 GB-month
LIMIT_CLASS_A = 1_000_000               # per month
LIMIT_CLASS_B = 10_000_000              # per month

_OPS_QUERY = (
    "query($a:String!,$s:Time!){viewer{accounts(filter:{accountTag:$a}){"
    "r2OperationsAdaptiveGroups(limit:200,filter:{datetime_geq:$s})"
    "{dimensions{actionType} sum{requests}}}}}"
)


def classify(action: str) -> str:
    """
    "A", "B" or "free" for an R2 action name.

    Cloudflare documents the PRINCIPLE — Class A mutates state, Class B reads
    it — rather than a list this script can pin itself to, and new action
    types appear over time. So the rule is by verb, and anything unrecognised
    counts as Class A: the expensive class. An alert that over-counts warns
    early; one that under-counts is silent exactly when it matters.
    """
    a = (action or "").lower()
    if a.startswith(("delete", "abort")):
        return "free"
    if a.startswith(("get", "head")):
        return "B"
    return "A"


def sum_operations(payload: dict) -> tuple[int, int]:
    accounts = (payload.get("data") or {}).get("viewer", {}).get("accounts") or []
    if not accounts:
        raise ValueError("no accounts in the analytics response")
    groups = accounts[0].get("r2OperationsAdaptiveGroups")
    if groups is None:
        raise ValueError("no r2OperationsAdaptiveGroups in the analytics response")
    a = b = 0
    for g in groups:
        n = (g.get("sum") or {}).get("requests") or 0
        kind = classify((g.get("dimensions") or {}).get("actionType", ""))
        if kind == "A":
            a += n
        elif kind == "B":
            b += n
    return a, b


def sum_storage(payload: dict) -> tuple[int, int]:
    """(bytes, objects) currently stored, across every storage class."""
    result = payload.get("result") or {}
    total_bytes = total_objects = 0
    for tier in result.values():
        published = (tier or {}).get("published") or {}
        total_bytes += (published.get("payloadSize") or 0) + (published.get("metadataSize") or 0)
        total_objects += published.get("objects") or 0
    return total_bytes, total_objects


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000.0
    return f"{n} B"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=90.0)
    args = ap.parse_args()

    token = os.environ.get("CF_TOKEN", "")
    account = os.environ.get("CF_ACCOUNT", "")
    if not token or not account:
        print("::error::CF_TOKEN or CF_ACCOUNT is not set.")
        return 1

    import httpx
    headers = {"Authorization": f"Bearer {token}"}

    try:
        r = httpx.get(f"{API}/accounts/{account}/r2/metrics", headers=headers, timeout=60.0)
        metrics = r.json()
        if not metrics.get("success"):
            raise ValueError(metrics.get("errors"))
        used_bytes, objects = sum_storage(metrics)
    except Exception as e:
        print(f"::error::Could not read R2 metrics ({e}). "
              f"Does the token carry 'Workers R2 Storage: Read'?")
        return 1

    since = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = httpx.post(f"{API}/graphql", headers=headers, timeout=60.0,
                       json={"query": _OPS_QUERY, "variables": {"a": account, "s": since}})
        class_a, class_b = sum_operations(r.json())
    except Exception as e:
        print(f"::error::Could not read R2 operation analytics ({e}). "
              f"Does the token carry 'Account Analytics: Read'?")
        return 1

    rows = [
        ("Storage", used_bytes, LIMIT_BYTES, f"{human(used_bytes)} ({objects} objects)", "10 GB"),
        ("Class A ops (month)", class_a, LIMIT_CLASS_A, f"{class_a:,}", "1,000,000"),
        ("Class B ops (month)", class_b, LIMIT_CLASS_B, f"{class_b:,}", "10,000,000"),
    ]

    lines = ["### R2 free-tier usage", "", "| Dimension | Used | Limit | % |", "|---|---:|---:|---:|"]
    over = []
    for name, used, limit, used_s, limit_s in rows:
        pct = (used * 100.0 / limit) if limit else 0.0
        lines.append(f"| {name} | {used_s} | {limit_s} | {pct:.2f}% |")
        print(f"{name}: {used_s} of {limit_s} = {pct:.2f}%")
        if pct >= args.threshold:
            over.append((name, pct))
    lines += ["", f"Alarm threshold: {args.threshold:g}%. "
                  f"Operation counts are for the current calendar month (since {since})."]

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    for name, pct in over:
        print(f"::error::R2 {name} is at {pct:.2f}% of the free tier.")
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
