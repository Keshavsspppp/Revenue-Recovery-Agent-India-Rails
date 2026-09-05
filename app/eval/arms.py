"""Randomisation into treatment and holdout.

Assignment happens at the failure event, before anything else touches the account, and is
written as the ASSIGN ledger event. Assigning or reading the arm later is how holdouts get
silently corrupted — the ledger refuses any other event for an account that has no ASSIGN.

Stratified, because simple randomisation on 2,000 accounts can imbalance the terminal
cause classes badly enough to swamp the effect being measured.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.domain.enums import Arm, CauseClass, MerchantCategory
from app.domain.money import amount_band


@dataclass(frozen=True)
class Stratum:
    """The three dimensions that most affect recoverability, per docs/08-EVALUATION.md."""

    cause: CauseClass
    band: str
    category: MerchantCategory

    def key(self) -> tuple[str, str, str]:
        return (self.cause.value, self.band, self.category.value)


def stratum_of(cause: CauseClass, amount_paise: int,
               category: MerchantCategory) -> Stratum:
    return Stratum(cause=cause, band=amount_band(amount_paise), category=category)


def assign_arms(strata: dict[str, Stratum], holdout_frac: float, seed: int,
                ) -> dict[str, Arm]:
    """Deterministic given the batch seed, so arms are reproducible from the seed alone.

    Within each stratum this is systematic sampling with a random start: walk the
    shuffled members and take one whenever a running counter crosses an integer. That
    keeps the holdout share close to `holdout_frac` inside *every* stratum, including the
    small ones — a plain `round(n * frac)` would give a stratum of 3 accounts zero
    holdouts every time, which biases the control group toward common causes.
    """
    if not 0.0 < holdout_frac < 1.0:
        raise ValueError(f"holdout_frac must be in (0, 1), got {holdout_frac}")

    # A dedicated sub-stream: changing the holdout fraction must not reshuffle the world.
    rng = np.random.default_rng([seed, 0xA12])
    groups: dict[tuple[str, str, str], list[str]] = {}
    for account_id, stratum in sorted(strata.items()):
        groups.setdefault(stratum.key(), []).append(account_id)

    arms: dict[str, Arm] = {}
    for key in sorted(groups):
        members = list(groups[key])
        rng.shuffle(members)
        counter = float(rng.random())          # random start, so small strata contribute
        for account_id in members:
            before = int(counter)
            counter += holdout_frac
            arms[account_id] = (Arm.HOLDOUT if int(counter) > before else Arm.TREATMENT)
    return arms


def balance_table(strata: dict[str, Stratum], arms: dict[str, Arm],
                  ) -> list[tuple[str, str, str, int, int]]:
    """arm x cause x band counts, so imbalance is visible rather than assumed away."""
    rows: dict[tuple[str, str, str], list[int]] = {}
    for account_id, stratum in strata.items():
        key = (stratum.cause.value, stratum.band, stratum.category.value)
        counts = rows.setdefault(key, [0, 0])
        counts[0 if arms[account_id] is Arm.TREATMENT else 1] += 1
    return [(*key, t, h) for key, (t, h) in sorted(rows.items())]


def chi_square_imbalance(strata: dict[str, Stratum], arms: dict[str, Arm]) -> float:
    """Pearson chi-square of holdout share across cause classes against the batch-wide
    share. Large values mean the randomisation drifted and the lift is not comparable."""
    by_cause: dict[str, list[int]] = {}
    for account_id, stratum in strata.items():
        counts = by_cause.setdefault(stratum.cause.value, [0, 0])
        counts[0 if arms[account_id] is Arm.TREATMENT else 1] += 1
    total_t = sum(c[0] for c in by_cause.values())
    total_h = sum(c[1] for c in by_cause.values())
    total = total_t + total_h
    if total == 0 or total_h == 0:
        return 0.0
    stat = 0.0
    for treatment, holdout in by_cause.values():
        n = treatment + holdout
        for observed, expected in ((treatment, n * total_t / total),
                                   (holdout, n * total_h / total)):
            if expected > 0:
                stat += (observed - expected) ** 2 / expected
    return stat
