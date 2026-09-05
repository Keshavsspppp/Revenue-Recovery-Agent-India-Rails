"""The hardship detector: observable signals in, a score out.

RBI's draft recovery norms expect a lender to identify borrowers in repayment difficulty
and offer guidance rather than another attempt. An agent that knows who *not* to chase is
the most differentiated thing in this build, and it is also the part where being wrong is
worst in both directions: miss someone and you hound a person who cannot pay, flag
someone wrongly and you write off money that was collectable.

So the score is additive and capped rather than fitted. Every weight is one legible claim
about what a signal is worth, which is what has to survive the question "why did you stop
chasing this account?". Its precision and recall against `latent_truth` are reported in
the evaluator — free to compute, and directly relevant to conduct.

Nothing here may read `latent.hardship`. That is the answer, not an input.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.config import Config
from app.domain.enums import CauseClass, PTPStatus

#: Codes that mean "the money was not there". The observable half of hardship.
SHORTFALL = frozenset({CauseClass.INSUFFICIENT_FUNDS})


@dataclass(frozen=True)
class HardshipSignals:
    """What we can actually see. Assembled from the ledger and the account's own state."""

    insufficient_funds_count: int = 0
    other_mandate_failing: bool = False
    distress_language: bool = False
    broken_promise: bool = False

    @property
    def broken_promise_then_shortfall(self) -> bool:
        """The strongest pair in the set: they committed to a date, missed it, and then
        came up short again. Neither signal alone says as much."""
        return self.broken_promise and self.insufficient_funds_count >= 1


def signals_from(codes: tuple[str, ...], codemap, other_mandate_failing: bool,
                 distress_language: bool, ptp_status: PTPStatus | None) -> HardshipSignals:
    return HardshipSignals(
        insufficient_funds_count=sum(
            1 for c in codes if codemap.cause_of(c) in SHORTFALL),
        other_mandate_failing=other_mandate_failing,
        distress_language=distress_language,
        broken_promise=ptp_status in (PTPStatus.BROKEN, PTPStatus.PARTIAL))


def score(signals: HardshipSignals, cfg: Config) -> float:
    """0..1. Additive, capped, and every term named in config."""
    w = cfg.raw["hardship"]["weights"]
    total = 0.0
    if signals.insufficient_funds_count >= 2:
        total += float(w["repeated_insufficient_funds"])
    if signals.other_mandate_failing:
        total += float(w["other_mandate_failing"])
    if signals.distress_language:
        total += float(w["distress_language"])
    if signals.broken_promise:
        total += float(w["broken_promise"])
    if signals.broken_promise_then_shortfall:
        total += float(w["broken_promise_then_shortfall"])
    return min(1.0, total)


def explain(signals: HardshipSignals, value: float) -> tuple[str, ...]:
    """Evidence for the ledger. An account routed out of recovery has to be able to say
    which observations put it there."""
    fired = []
    if signals.insufficient_funds_count >= 2:
        fired.append(f"repeated_insufficient_funds:{signals.insufficient_funds_count}")
    if signals.other_mandate_failing:
        fired.append("other_mandate_failing:1")
    if signals.distress_language:
        fired.append("distress_language:1")
    if signals.broken_promise:
        fired.append("broken_promise:1")
    if signals.broken_promise_then_shortfall:
        fired.append("broken_promise_then_shortfall:1")
    return (f"hardship_score:{value:.2f}", *fired)
