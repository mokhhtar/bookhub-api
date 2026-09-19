"""Validate the shipped mind-reader question policy without changing it.

Run before every promotion that adds, removes, or reclassifies a question:

    python scripts/akinator/audit_question_policy.py

The registry is intentionally checked against the artifacts the browser
actually loads.  A source-code vocabulary can contain dropped questions;
requiring policy for those would make the audit describe a different game.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ARTIFACTS = os.path.abspath(os.path.join(
    REPO_ROOT, "..", "bookhub", "games", "data", "akinator"))
LEVELS = {"core", "general", "specific", "endgame", "experimental"}
NARRATIVE_PATTERNS = (
    r"\binvented world\b", r"\bpower system\b", r"\bsupernatural\b",
    r"\bfantasy\b", r"\bscience fiction\b", r"\bweb novel\b",
    r"\blight novel\b",
)


def _load(path: str, name: str):
    with open(os.path.join(path, name), encoding="utf-8") as fh:
        return json.load(fh)


def _condition_edges(condition, child: str, live: set[str], errors: list[str],
                     where: str) -> list[tuple[str, str]]:
    if not isinstance(condition, dict):
        errors.append(f"{where}: condition must be an object")
        return []
    operators = [key for key in ("any", "all") if key in condition]
    if operators:
        if len(operators) != 1 or len(condition) != 1:
            errors.append(f"{where}: use exactly one of any/all")
            return []
        key = operators[0]
        rows = condition[key]
        if not isinstance(rows, list) or not rows:
            errors.append(f"{where}.{key}: must be a non-empty list")
            return []
        out: list[tuple[str, str]] = []
        for i, row in enumerate(rows):
            out.extend(_condition_edges(row, child, live, errors,
                                        f"{where}.{key}[{i}]"))
        return out

    if set(condition) != {"question", "answer"}:
        errors.append(f"{where}: leaf must contain only question and answer")
        return []
    parent, answer = condition.get("question"), condition.get("answer")
    if parent not in live:
        errors.append(f"{where}: unknown parent {parent!r}")
    if parent == child:
        errors.append(f"{where}: a question cannot require itself")
    if answer not in ("yes", "no"):
        errors.append(f"{where}: answer must be yes or no")
    return [(parent, child)] if isinstance(parent, str) else []


def _cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    graph: dict[str, set[str]] = {}
    for parent, child in edges:
        graph.setdefault(parent, set()).add(child)
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def walk(node: str) -> list[str] | None:
        if node in visiting:
            start = path.index(node)
            return path[start:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        path.append(node)
        for child in graph.get(node, ()):
            found = walk(child)
            if found:
                return found
        path.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in graph:
        found = walk(node)
        if found:
            return found
    return None


def audit(path: str) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        questions = _load(path, "questions.json")
        cold = _load(path, "cold_questions.json")
        policy = _load(path, "question_policy.json")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read shipped artifacts: {exc}"], [], {}

    live_rows = [row for row in questions + cold if isinstance(row, dict)]
    live = {row.get("id") for row in live_rows if isinstance(row.get("id"), str)}
    wording = {row["id"]: str(row.get("text") or "") for row in live_rows
               if isinstance(row.get("id"), str)}
    if not isinstance(policy, dict) or policy.get("version") != 1:
        errors.append("question_policy.json must be an object with version 1")
        return errors, warnings, {}
    entries = policy.get("questions")
    if not isinstance(entries, dict):
        errors.append("question_policy.json.questions must be an object")
        return errors, warnings, {}

    missing = sorted(live - set(entries))
    extra = sorted(set(entries) - live)
    if missing:
        errors.append("live questions missing policy: " + ", ".join(missing))
    if extra:
        errors.append("policy refers to non-live questions: " + ", ".join(extra))

    edges: list[tuple[str, str]] = []
    gated = 0
    for qid, entry in entries.items():
        where = f"questions.{qid}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: entry must be an object")
            continue
        if entry.get("level") not in LEVELS:
            errors.append(f"{where}.level: invalid value {entry.get('level')!r}")
        if not isinstance(entry.get("domain"), str) or not entry.get("domain"):
            errors.append(f"{where}.domain: non-empty string required")
        cost = entry.get("answerability_cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost <= 1:
            errors.append(f"{where}.answerability_cost: number from 0 to 1 required")
        condition = entry.get("applies_if")
        if condition is not None:
            gated += 1
            edges.extend(_condition_edges(condition, qid, live, errors,
                                          f"{where}.applies_if"))
        elif entry.get("level") != "core" and any(
                re.search(pattern, wording.get(qid, "").lower())
                for pattern in NARRATIVE_PATTERNS):
            warnings.append(f"{qid}: narrative wording has no applies_if gate")
        skip_condition = entry.get("skip_if")
        if skip_condition is not None:
            edges.extend(_condition_edges(skip_condition, qid, live, errors,
                                          f"{where}.skip_if"))

    cycle = _cycle(edges)
    if cycle:
        errors.append("applicability cycle: " + " -> ".join(cycle))
    stats = {"live": len(live), "registered": len(entries),
             "gated": gated, "edges": len(edges)}
    return errors, warnings, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", default=DEFAULT_ARTIFACTS)
    args = parser.parse_args()
    errors, warnings, stats = audit(args.artifacts)
    print("question policy:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        sys.exit(1)
    print(f"POLICY OK ({len(warnings)} warning(s))")


if __name__ == "__main__":
    main()
