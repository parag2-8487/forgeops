# SPDX-License-Identifier: FSL-1.1-ALv2
"""Invariant 3: the score generation can reach, stated before it runs and checked after.

WHY A CONTRACT AND NOT A MESSAGE

The readiness screen used to carry a blanket "generation cannot raise this score" for anything it was
unsure about, which is the least useful thing it could say: it is wrong for most checks and it gives an
operator no way to tell "the platform will fix this" from "somebody has to sit down and do it".

So the product now commits to a number. Before generation it says: this is the score now, this is the
score a run can reach, and here is every point it cannot reach with the reason for each. After the
apply the rescan is compared against that prediction, and a gap is a DEFECT the product reports rather
than a footnote in a report - the whole value of a prediction is that being wrong about it is visible.

HOW THE NUMBER IS DERIVED

From the same two tables everything else in this area uses, never a copy:

  * `CHECK_EXPLANATIONS[id].generatable_today` - the fix is a file that can be written AND the
    generator actually emits that kind. Both halves, because reporting the first alone had the screen
    offering a CI workflow the generator could not produce.
  * `Fixability.blocked_because` - the reason, written for an operator, when it cannot.

The reachable score is what the engine would report if every generatable failing check passed and every
non-generatable one stayed exactly where it is. It is deliberately NOT optimistic about partial credit:
a check earning 1 of 15 that generation can fix is counted at its maximum, because generation replaces
the artifact rather than improving it in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .readiness_findings import CHECK_EXPLANATIONS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .readiness import ReadinessResult


@dataclass(frozen=True)
class UnreachablePoint:
    """A check generation cannot fix, and why."""

    check_id: str
    category: str
    points_short: int
    reason: str

    def __str__(self) -> str:
        return f"{self.check_id} ({self.points_short} point(s)): {self.reason}"


@dataclass(frozen=True)
class ReachableScore:
    """What generation can and cannot do to this project's score."""

    current: int
    reachable: int
    #: Checks generation would fix, with the points each would recover.
    fixable: tuple[tuple[str, int], ...]
    #: Checks it cannot, each with a reason that is about the project, never about effort.
    unreachable: tuple[UnreachablePoint, ...]

    @property
    def gain(self) -> int:
        return self.reachable - self.current

    def summary(self) -> str:
        if not self.fixable:
            return (
                f"Generation cannot raise this score above {self.current}. "
                f"{len(self.unreachable)} failing check(s) need work it cannot do."
            )
        return (
            f"Generation can take this from {self.current} to {self.reachable} by producing "
            f"{len(self.fixable)} artifact(s). {len(self.unreachable)} check(s) would remain."
        )


def reachable_score(result: ReadinessResult) -> ReachableScore:
    """The score a generation run can reach from this result, and what it cannot touch.

    THE ARITHMETIC IS THE ENGINE'S OWN, not a re-weighting. Each check carries `points` and
    `max_points`, and the overall score is a weighted mean over categories - so the reachable score is
    computed by asking the engine's own category weights what the total becomes when the generatable
    checks are full. Re-deriving the weighting here would produce a second scoring model, and the two
    would disagree the first time a weight changed.
    """
    from .readiness import CATEGORY_WEIGHTS

    fixable: list[tuple[str, int]] = []
    unreachable: list[UnreachablePoint] = []

    # Per category: what is earned now, what is available, and what generation could add.
    earned: dict[str, int] = {}
    available: dict[str, int] = {}
    recoverable: dict[str, int] = {}

    for check in result.checks:
        category = check.category
        earned[category] = earned.get(category, 0) + check.points
        available[category] = available.get(category, 0) + check.max_points
        recoverable.setdefault(category, 0)

        short = check.max_points - check.points
        if short <= 0:
            continue

        explanation = CHECK_EXPLANATIONS.get(check.id)
        if explanation is not None and explanation.generatable_today:
            fixable.append((check.id, short))
            recoverable[category] += short
        else:
            reason = ""
            if explanation is not None:
                reason = explanation.fixability.blocked_because or ""
            if not reason:
                # Never an empty reason and never "not supported". An operator reading this has to be
                # able to act on it or knowingly accept it.
                reason = (
                    "no single generated artifact satisfies this check; it depends on code or on a "
                    "decision this platform cannot make for you"
                )
            unreachable.append(
                UnreachablePoint(
                    check_id=check.id,
                    category=category,
                    points_short=short,
                    reason=reason,
                )
            )

    def weighted(scores: dict[str, int]) -> int:
        total = 0.0
        for category, weight in CATEGORY_WEIGHTS.items():
            if not available.get(category):
                continue
            total += weight * (scores.get(category, 0) / available[category])
        return round(total)

        # A category with nothing available contributes nothing rather than a division by zero. The
        # engine makes the same choice, which is why the two numbers agree.

    after = {category: earned.get(category, 0) + recoverable.get(category, 0) for category in available}

    return ReachableScore(
        current=result.overall_score,
        reachable=weighted(after),
        fixable=tuple(sorted(fixable)),
        unreachable=tuple(sorted(unreachable, key=lambda item: (-item.points_short, item.check_id))),
    )


@dataclass(frozen=True)
class PredictionOutcome:
    """Predicted against achieved, after an apply and a rescan."""

    predicted: int
    achieved: int

    @property
    def matched(self) -> bool:
        return self.predicted == self.achieved

    def describe(self) -> str:
        if self.matched:
            return f"predicted {self.predicted} and achieved {self.predicted}"
        direction = "short of" if self.achieved < self.predicted else "above"
        return (
            f"predicted {self.predicted} but achieved {self.achieved} - "
            f"{abs(self.predicted - self.achieved)} point(s) {direction} the prediction. "
            f"This is a defect in the prediction or in the generation, not a rounding difference."
        )
