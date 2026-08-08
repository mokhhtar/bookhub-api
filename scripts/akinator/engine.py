"""
scripts/akinator/engine.py — the inference engine.

WHAT AKINATOR ACTUALLY IS (from [[Akinator for books — requirements]]):
not a stored decision tree but a **Bayesian belief update over a candidate
set**, with questions chosen by expected information gain.

  1. A matrix of probabilities, not facts: P(yes | book, question).
  2. After each answer: P(book | answer) proportional to
     P(answer | book) * P(book), the prior being popularity.
  3. Ask whichever question splits the current probability mass closest to
     half — that is all the apparent cleverness is.
  4. Guess when the leader crosses a confidence threshold.

Point 2 is why a wrong answer *weakens* a candidate instead of eliminating
it, and therefore why the game survives a player's mistakes and their
"probably"s. Nothing here is ever hard-filtered out of the candidate set.

TWO QUESTION POOLS. Subject/fact questions carry the early game. Character
questions ("is one of them named Poirot?") are held back for the endgame —
with 20k candidates a name splits off a handful and wastes a turn, but with
30 candidates left it is decisive, and it is the moment that makes the game
feel like it read your mind.

This module is deliberately dependency-free: the same arithmetic has to run
in JavaScript in the browser at play time, so it must not lean on anything
numpy-shaped that won't port.
"""
from __future__ import annotations

import math

from features import (EXCLUDES, PRESENCE_CONFIDENCE, UNKNOWN_CONFIDENCE,
                      absence_confidence)
from series import VOLUME_DOMINANCE

# The five answers a player can give, and how strongly each is trusted.
# "probably" answers move belief in the same direction as a firm answer but
# less far, which is what makes the scale worth having at all.
ANSWER_WEIGHTS = {
    "yes": 1.0,
    "probably_yes": 0.65,
    "unknown": 0.0,
    "probably_no": -0.65,
    "no": -1.0,
}

# Belief floor. No candidate's likelihood is ever allowed to reach zero, so
# a book can always climb back after a player answers something wrong.
MIN_LIKELIHOOD = 0.02

# A named character the record does not list is decent evidence — OL's cast
# lists are reasonably complete when present. But never conclusive.
CHAR_ABSENT_CONFIDENCE = 0.06

# What "we don't know this book's cast" means for "is one of them named X?".
#
# MEASURED CORRECTION, and the most useful thing phase 0 found. This was
# 0.5 — the requirements note's "unknown = 0.5" rule, applied literally.
# It cost books with no recorded cast 20 points of success rate (56% -> 36%)
# the moment character questions were switched on, which is the opposite of
# what a neutral value is supposed to do.
#
# The reason: 0.5 is only neutral for a feature that is a coin flip a
# priori. "Is one of them named Hermione?" is not — by construction a
# character token appears in under 2% of books. So when the player says no,
# a book whose cast is known-without-Hermione scores 0.94 while a book with
# no cast data scores 0.5, and the unknown book is punished for the
# library's gap rather than left alone. "Unknown" has to mean **the base
# rate**, not a coin flip; only then does an absent record neither help nor
# hurt. Slightly above CHAR_ABSENT_CONFIDENCE, because this book genuinely
# might have the character and simply not be catalogued.
CHAR_UNKNOWN_CONFIDENCE = 0.10

# Character questions only once the field has narrowed this far.
ENDGAME_CANDIDATES = 30

# How sure the engine must be that the player's book HAS a named cast before
# it spends endgame turns asking about names. Swept, not guessed — measured
# on 25 books with a recorded cast and 25 without:
#
#   gate   has cast          no cast
#   0.50   84% (16q)   +8    44% (22q)  -12
#   0.75   84% (16q)   +8    52% (25q)   -4     <- chosen
#   0.90   76% (18q)    0    56% (25q)    0     (never opens; same as off)
#
# 0.75 keeps the entire gain for books we have a cast for while cutting the
# cost to books we don't by two thirds. Deltas are against the same run with
# character questions disabled (76% / 56%).
NAMED_CHARS_GATE = 0.75


class Matrix:
    """Precomputed P(yes | book, question) for every book and question.

    Built once and shared across games. It is static data — rebuilding it
    per game was the prototype's whole runtime cost.
    """

    def __init__(self, books: list[dict], questions: list[str],
                 char_questions: list[str] | None = None,
                 series_of: list[str | None] | None = None,
                 series_names: dict[str, str] | None = None):
        self.books = books
        # Series membership per book index, for guess-time pooling. Absent
        # is fine — the engine simply never guesses a series.
        self.series_of = series_of or [None] * len(books)
        self.series_names = series_names or {}
        self.series_members: dict[str, list[int]] = {}
        for _i, _sid in enumerate(self.series_of):
            if _sid:
                self.series_members.setdefault(_sid, []).append(_i)
        self.questions = questions
        self.question_set = set(questions)
        self.char_questions = char_questions or []
        self.char_question_set = set(self.char_questions)

        # token -> indices of books whose cast contains it. Lets the endgame
        # consider only names some live candidate actually has, instead of
        # scoring every name in the corpus on every turn.
        self.char_index: dict[str, set[int]] = {}
        for i, book in enumerate(books):
            for token in book.get("char_tokens") or ():
                self.char_index.setdefault(token, set()).add(i)

        self.rows: list[dict[str, float]] = []
        for book in books:
            present = set(book["present"])
            unknown = set(book["unknown"])
            absent_p = absence_confidence(book["richness"])
            tokens = set(book.get("char_tokens") or ())
            has_char_data = bool(tokens)

            row: dict[str, float] = {}
            for q in questions:
                if q in present:
                    row[q] = PRESENCE_CONFIDENCE
                elif q in unknown:
                    # Value is a placeholder; unknown cells are handled by
                    # `unknown_at` during the update and never read here.
                    row[q] = UNKNOWN_CONFIDENCE
                else:
                    row[q] = absent_p
            for q in self.char_questions:
                token = q.split(":", 1)[1]
                if token in tokens:
                    row[q] = PRESENCE_CONFIDENCE
                elif has_char_data:
                    row[q] = CHAR_ABSENT_CONFIDENCE
                else:
                    # No cast recorded at all. Absence here is not a denial —
                    # scoring it as "no" would eliminate every book whose
                    # characters OL simply never catalogued. See the constant
                    # for why this is a base rate and not 0.5.
                    row[q] = CHAR_UNKNOWN_CONFIDENCE
            self.rows.append(row)

        # Prior: popularity, log-compressed. Raw readinglog_count spans
        # 62,023 (Atomic Habits) to single digits; used linearly it would
        # make the top few books unbeatable regardless of answers.
        priors = [math.log1p(b["popularity"]) + 1.0 for b in books]
        total = sum(priors)
        self.prior = [p / total for p in priors]


class Engine:
    """Belief state over a candidate set of books."""

    def __init__(self, matrix: Matrix):
        self.m = matrix
        self.belief = list(matrix.prior)
        self.asked: set[str] = set()

    # -- inference ---------------------------------------------------------

    def update(self, question: str, answer: str) -> None:
        """Fold one answer into the belief state."""
        self.asked.add(question)
        weight = ANSWER_WEIGHTS[answer]
        if answer == "yes":
            # A firm yes rules out the questions that cannot also be true.
            # Marked asked rather than deleted, so "go back" restores them
            # with the rest of the state.
            #
            # ONLY a firm yes. "Probably American" is a hedge, and
            # suppressing the siblings on a hedge throws away real
            # information — the A/B below is against no exclusions at all,
            # same games and seeds, 5,000 books:
            #
            #   off              58.3%  median 24q
            #   on, any positive 53.3%  median 28q
            #
            # Five points is the cost of never asking a contradictory
            # question, and it is worth paying. The simulated player does
            # not mind being asked whether an American author is African;
            # a real one stops believing the game is reading their mind,
            # and that belief is the entire product. Third time this
            # simulation has been blind to the thing that matters — see
            # phase 3 on unanswerable questions.
            self.asked.update(EXCLUDES.get(question, ()))
        if weight == 0.0:
            return  # "don't know" genuinely tells us nothing; don't fake a signal

        # UNKNOWN STAYS AT 0.5, and this is an empirical result that
        # overrode two more principled-looking alternatives. Both were
        # measured at 5,000 books against 0.5's 63.3% / 23q:
        #
        #   per-question base rate  55.0% / 19q
        #   the mean likelihood     41.7% / 18q   (genuinely neutral)
        #
        # The base rate punishes an unknown book whenever the player answers
        # YES — the mirror image of the phase 0 finding that 0.5 punishes it
        # on NO for a rare feature. No constant is neutral to both.
        #
        # The mean likelihood IS neutral, and is worse still, which is the
        # interesting part: a book that can never be eliminated accumulates
        # at the top and crowds out the answer. 0.5 quietly does something
        # useful instead — it lets thinly documented books drift down, and
        # thinly documented correlates with obscure, which correlates with
        # "not what the player is thinking of".
        total = 0.0
        for i, row in enumerate(self.m.rows):
            p = row[question]
            # Interpolate between "they said yes" and "they said no"
            # according to how firm the answer was.
            if weight > 0:
                likelihood = (1 - weight) * 0.5 + weight * p
            else:
                likelihood = (1 + weight) * 0.5 + (-weight) * (1 - p)
            self.belief[i] *= max(likelihood, MIN_LIKELIHOOD)
            total += self.belief[i]

        if total > 0:
            self.belief = [b / total for b in self.belief]

    def reject(self, index: int) -> None:
        """The player said our guess was wrong. Weaken, never erase."""
        self.belief[index] *= 0.001
        total = sum(self.belief)
        if total > 0:
            self.belief = [b / total for b in self.belief]

    # -- question selection ------------------------------------------------

    @staticmethod
    def _entropy(weights: list[float]) -> float:
        total = sum(weights)
        if total <= 0:
            return 0.0
        h = 0.0
        for w in weights:
            if w > 0:
                p = w / total
                h -= p * math.log2(p)
        return h

    def effective_candidates(self, mass: float = 0.9) -> int:
        """How many books still hold most of the belief."""
        ordered = sorted(self.belief, reverse=True)
        acc, n = 0.0, 0
        for b in ordered:
            acc += b
            n += 1
            if acc >= mass:
                break
        return n

    def _expects_named_characters(self, threshold: float = NAMED_CHARS_GATE) -> bool:
        """Does current belief say the player's book even has a named cast?

        The gate that stops character questions from wasting the endgame on
        a memoir or a physics textbook. `fact:namedchars` is already one of
        the ordinary questions, so by the time the endgame arrives the
        player has usually answered it — this just reads the answer back
        out of the belief state instead of asking again.

        Without this gate, switching character questions on cost books with
        no recorded cast 16 points of success rate: every name asked was a
        turn spent learning nothing about their book.
        """
        q = "fact:namedchars"
        if q not in self.m.question_set:
            return True
        return sum(b * row[q] for b, row in zip(self.belief, self.m.rows)) >= threshold

    def _pool(self) -> list[str]:
        """Which questions are worth scoring this turn.

        Subject questions always. Character questions only in the endgame,
        and then only names carried by a book still in contention — a name
        no live candidate has cannot split anything, and scoring the whole
        corpus's cast every turn is what made the first prototype
        unrunnable.
        """
        pool = list(self.m.questions)
        if not self.m.char_questions:
            return pool
        if self.effective_candidates() > ENDGAME_CANDIDATES:
            return pool
        if not self._expects_named_characters():
            return pool

        live = {idx for idx, _p in self.ranking(ENDGAME_CANDIDATES)}
        tokens: set[str] = set()
        for idx in live:
            tokens.update(self.m.books[idx].get("char_tokens") or ())
        pool += [f"char:{t}" for t in tokens
                 if f"char:{t}" in self.m.char_question_set]
        return pool

    def next_question(self, exact: bool = False) -> str | None:
        """The most informative unasked question.

        Two implementations of the same idea. `exact` computes the real
        expected information gain — the entropy after a yes and after a no,
        weighted by how likely each answer is. The default computes the
        belief-weighted P(yes) and picks whichever question lands closest
        to half, which is the formulation the requirements note describes
        and a well-known approximation of the same thing.

        The approximation is the default because of cost, and the cost is
        not academic: exact gain makes six passes over the candidate set per
        question and allocates two lists each time, so at 5,000 books a
        single simulated game took long enough to time out a ten-minute
        run. The fast path makes ONE pass over the books, accumulating
        every question's total as it goes, and allocates nothing.

        The same arithmetic has to run in the browser on every turn, so the
        cheap version is what ships; `exact=True` stays for offline
        comparison of the two.
        """
        pool = [q for q in self._pool() if q not in self.asked]
        if not pool:
            return None

        if exact:
            return self._next_question_exact(pool)

        # One pass over books, accumulating P(yes) for every question at
        # once. Question-major would re-walk the belief vector per question.
        totals = dict.fromkeys(pool, 0.0)
        for b, row in zip(self.belief, self.m.rows):
            if b <= 0.0:
                continue
            for q in pool:
                totals[q] += b * row[q]

        best, best_dist = None, 2.0
        for q, p_yes in totals.items():
            if p_yes <= 0.02 or p_yes >= 0.98:
                continue  # everyone answers the same way; asking wastes a turn
            dist = abs(p_yes - 0.5)
            if dist < best_dist:
                best, best_dist = q, dist
        return best

    def _next_question_exact(self, pool: list[str]) -> str | None:
        base = self._entropy(self.belief)
        best, best_gain = None, -1.0
        rows, belief = self.m.rows, self.belief

        for q in pool:
            p_yes = sum(b * row[q] for b, row in zip(belief, rows))
            if p_yes <= 0.02 or p_yes >= 0.98:
                continue
            yes_branch = [b * row[q] for b, row in zip(belief, rows)]
            no_branch = [b - y for b, y in zip(belief, yes_branch)]
            expected = (p_yes * self._entropy(yes_branch)
                        + (1 - p_yes) * self._entropy(no_branch))
            gain = base - expected
            if gain > best_gain:
                best, best_gain = q, gain
        return best

    # -- reading the state -------------------------------------------------

    def ranking(self, n: int = 5) -> list[tuple[int, float]]:
        """(book index, probability), most likely first."""
        pairs = sorted(enumerate(self.belief), key=lambda x: -x[1])
        return pairs[:n]

    # -- guessing, pooled by series ----------------------------------------

    def series_belief(self) -> dict[str, float]:
        """Total belief held by each series."""
        out: dict[str, float] = {}
        for sid, members in self.m.series_members.items():
            out[sid] = sum(self.belief[i] for i in members)
        return out

    def guess_target(self, rejected: set | None = None) -> dict | None:
        """What to name: a single book, or a whole series.

        A series is named when the group has the belief but no volume in it
        does. That is the case the owner hit — twelve Harry Potter rows each
        holding a twelfth of the mass, so the game either kept asking or
        eventually named a volume nobody was thinking of. "Harry Potter" is
        never a wrong answer to someone thinking of Harry Potter.

        A volume is still named when it genuinely dominates its group, so a
        player who really did mean *Prisoner of Azkaban* can still get it.
        """
        rejected = rejected or set()

        ranked = [(i, p) for i, p in self.ranking(20) if i not in rejected]
        if not ranked:
            return None
        top_idx, top_p = ranked[0]

        if top_p >= 0.85:
            return {"kind": "book", "index": top_idx, "p": top_p}

        totals = self.series_belief()
        for sid, total in sorted(totals.items(), key=lambda x: -x[1]):
            if sid in rejected or total < 0.85:
                continue
            members = [i for i in self.m.series_members[sid] if i not in rejected]
            if not members:
                continue
            best = max(members, key=lambda i: self.belief[i])
            if self.belief[best] / total >= VOLUME_DOMINANCE:
                return {"kind": "book", "index": best, "p": self.belief[best]}
            return {"kind": "series", "sid": sid,
                    "name": self.m.series_names.get(sid, ""),
                    "index": best, "p": total}

        return {"kind": "book", "index": top_idx, "p": top_p}

    def should_guess(self, threshold: float = 0.85) -> bool:
        """Guess on absolute confidence, or on a decisive lead.

        The ratio test matters when two books are genuinely similar: 0.55
        against 0.06 is a confident guess even though it never reaches
        0.85, and refusing to guess there just burns questions the player
        experiences as the game stalling.
        """
        top = self.ranking(2)
        if top[0][1] >= threshold:
            return True
        # A series holding the mass counts as being ready to guess, even
        # when no single volume does — which is the whole point of pooling.
        if self.m.series_members:
            if max(self.series_belief().values(), default=0.0) >= threshold:
                return True
        if len(top) > 1 and top[1][1] > 0:
            return top[0][1] / top[1][1] >= 12 and top[0][1] >= 0.35
        return False
