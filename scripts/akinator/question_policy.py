"""Shared evaluation helpers for semantic question applicability.

The policy uses three-valued logic: a condition can be true, false, or
unresolved.  Only a definitely false condition produces NOT_APPLICABLE;
missing parent evidence must never be turned into a confident negative.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable


AnswerLookup = Callable[[str], bool | None]
STATE_ABSENT = 0
STATE_PRESENT = 1
STATE_UNKNOWN = 2
STATE_NOT_APPLICABLE = 3


def condition_value(condition: object, answer_for: AnswerLookup) -> bool | None:
    """Evaluate one ``applies_if`` expression with Kleene three-valued logic."""
    if not isinstance(condition, dict):
        return None

    question = condition.get("question")
    wanted = condition.get("answer")
    if isinstance(question, str) and wanted in ("yes", "no"):
        actual = answer_for(question)
        if actual is None:
            return None
        return actual is (wanted == "yes")

    for operator in ("any", "all"):
        children = condition.get(operator)
        if not isinstance(children, list) or not children:
            continue
        values = [condition_value(child, answer_for) for child in children]
        if operator == "any":
            if True in values:
                return True
            return False if all(value is False for value in values) else None
        if False in values:
            return False
        return True if all(value is True for value in values) else None
    return None


def book_answer_lookup(book: dict) -> AnswerLookup:
    """Return grounded yes/no/unknown answers for a corpus-style book row."""
    present = set(book.get("present") or ())
    unknown = set(book.get("unknown") or ())
    not_applicable = set(book.get("not_applicable") or ())
    known_false = set(book.get("known_false") or ())

    def answer(question: str) -> bool | None:
        if question in present:
            return True
        if question in unknown or question in not_applicable:
            return None
        if question in known_false:
            return False
        # The build's established representation is sparse: a computed
        # feature omitted from both sets is an absent/negative feature.
        return False

    return answer


def not_applicable_ids(book: dict, policy: dict | None,
                       question_ids: Iterable[str] | None = None) -> set[str]:
    """Questions whose applicability is definitely false for ``book``.

    A positive child cell wins over policy.  That contradiction should stay
    visible to audits instead of silently erasing grounded positive data.
    """
    entries = policy.get("questions") if isinstance(policy, dict) else None
    if not isinstance(entries, dict):
        return set(book.get("not_applicable") or ())

    allowed = set(question_ids) if question_ids is not None else None
    present = set(book.get("present") or ())
    answer_for = book_answer_lookup(book)
    result: set[str] = set()
    for question, entry in entries.items():
        if allowed is not None and question not in allowed:
            continue
        if question in present or not isinstance(entry, dict):
            continue
        condition = entry.get("applies_if")
        if condition is not None and condition_value(condition, answer_for) is False:
            result.add(question)
    return result


def policy_digest(policy: dict | None) -> str:
    """Stable semantic fingerprint, independent of JSON formatting."""
    canonical = json.dumps(policy or {}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def encode_not_applicable_matrix(
        matrix: bytes | bytearray, meta: dict, questions: list[dict],
        policy: dict, max_books: int,
        reset_questions: Iterable[str] = ()) -> tuple[bytes, dict]:
    """Return a packed matrix whose definitely closed cells use state 3.

    ``reset_questions`` is for a policy edit. Existing N/A cells in those
    columns are first downgraded to UNKNOWN because their original absent vs
    unknown state is unrecoverable; the new policy then safely re-derives the
    cells that remain inapplicable.
    """
    books = int(meta["books"])
    count = int(meta["questions"])
    bpr = int(meta["bytes_per_row"])
    if books > max_books:
        raise ValueError(f"refusing {books} books; max_books is {max_books}")
    if len(questions) != count:
        raise ValueError("questions.json count does not match meta.json")
    raw = bytearray(matrix)
    if len(raw) != books * bpr:
        raise ValueError("matrix.bin size does not match meta.json")

    qids = [q["id"] for q in questions]
    qindex = {qid: i for i, qid in enumerate(qids)}
    entries = policy.get("questions") if isinstance(policy, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    gated = [(i, entries[qid].get("applies_if"))
             for i, qid in enumerate(qids)
             if isinstance(entries.get(qid), dict)
             and entries[qid].get("applies_if") is not None]
    reset = {qindex[qid] for qid in reset_questions if qid in qindex}

    changed = conflicts = unresolved = reset_cells = stale = 0
    for book in range(books):
        offset = book * bpr
        states = [
            (raw[offset + (q >> 2)] >> ((q & 3) * 2)) & 3
            for q in range(count)
        ]
        for q in reset:
            if states[q] == STATE_NOT_APPLICABLE:
                states[q] = STATE_UNKNOWN
                reset_cells += 1
        # Applicability for every child is evaluated from the same grounded
        # row snapshot. Otherwise marking one child N/A could turn it into an
        # unknown parent for a later child and make the result depend on JSON
        # question order.
        source_states = states.copy()

        def answer_for(question: str) -> bool | None:
            index = qindex.get(question)
            if index is None:
                return None
            state = source_states[index]
            if state == STATE_PRESENT:
                return True
            if state == STATE_ABSENT:
                return False
            return None

        for q, condition in gated:
            applies = condition_value(condition, answer_for)
            if applies is None:
                unresolved += 1
                continue
            if applies:
                if states[q] == STATE_NOT_APPLICABLE:
                    stale += 1
                continue
            if states[q] == STATE_PRESENT:
                conflicts += 1
                continue
            if states[q] != STATE_NOT_APPLICABLE:
                states[q] = STATE_NOT_APPLICABLE
                changed += 1

        for q, state in enumerate(states):
            byte = offset + (q >> 2)
            shift = (q & 3) * 2
            raw[byte] = (raw[byte] & ~(3 << shift)) | (state << shift)

    stats = {"books": books, "questions": count, "gated": len(gated),
             "changed": changed, "conflicts": conflicts,
             "unresolved": unresolved, "reset": reset_cells, "stale": stale}
    return bytes(raw), stats
