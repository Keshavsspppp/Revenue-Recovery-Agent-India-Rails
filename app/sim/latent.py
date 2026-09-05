"""Latent customer state. Never exposed to the agent.

The one rule that makes the whole thing honest, from docs/03-SIMULATOR.md:

    A message can move `intent` and `annoyance`. A message can never move `balance`.

`apply_contact` below is the only place contact effects are applied, and it touches
neither `balance_paise` nor `inflow_day`. Every honest finding this project produces
comes from that asymmetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

import numpy as np

from app.domain.enums import CauseClass, Channel


@dataclass
class LatentAccount:
    """Generated once per account from the batch seed. Mutable: it evolves daily."""

    inflow_day: int              # 1..28, salary day of month
    inflow_paise: int            # monthly credit
    balance_paise: int           # evolves daily
    burn_rate: float             # fraction of inflow spent per day
    intent: float                # 0..1 — propensity to self-pay when prompted
    annoyance: float             # 0..1 — accumulates with contacts
    hardship: bool               # reduced / irregular inflow
    dispute_prone: bool
    mandate_defect: CauseClass | None
    responsiveness: float        # 0..1 — probability of answering voice / replying
    last_inflow_month: tuple[int, int] | None = None   # (year, month) already credited
    #: What actually went wrong on the first failure. Ground truth, for the evaluator to
    #: score the cause model against — never visible to the agent.
    true_cause: CauseClass | None = None

    def tick(self, day: date, rng: np.random.Generator, sim: Mapping[str, Any]) -> None:
        """One simulated day of balance dynamics. Produces the sawtooth that makes
        INSUFFICIENT_FUNDS a *timing* problem: nearly empty before payday, flush after."""
        stamp = (day.year, day.month)
        if day.day == self.inflow_day and self.last_inflow_month != stamp:
            self.last_inflow_month = stamp
            if not self.hardship:
                self.balance_paise += self.inflow_paise
            else:
                lo, hi = sim["hardship_inflow_fraction"]
                if rng.random() >= sim["hardship_skip_month_p"]:
                    self.balance_paise += int(self.inflow_paise * rng.uniform(lo, hi))
                # else: the month is skipped entirely
        # Poisson jitter on the daily burn, so spend is lumpy rather than a smooth ramp
        jitter = rng.poisson(10) / 10.0
        self.balance_paise -= int(self.inflow_paise * self.burn_rate * jitter)
        self.balance_paise = max(self.balance_paise, 0)


def generate_latent(rng: np.random.Generator, sim: Mapping[str, Any], city_tier: int,
                    ) -> LatentAccount:
    """Draws from the generative model declared in docs/03-SIMULATOR.md."""
    weights = [m[0] for m in sim["inflow_day_mixture"]]
    lo, hi = sim["inflow_day_mixture"][rng.choice(len(weights), p=weights)][1:]
    inflow_day = int(rng.integers(lo, hi + 1))

    ln = sim["inflow_paise_lognormal"]
    inflow_paise = int(np.exp(rng.normal(ln["mean_log_by_tier"][city_tier],
                                         ln["sigma_log"])) * 100)

    g = sim["burn_rate_gamma"]
    burn_rate = float(rng.gamma(g["k"], g["theta"]))

    return LatentAccount(
        inflow_day=inflow_day,
        inflow_paise=inflow_paise,
        balance_paise=0,                      # set by the batch generator, see below
        burn_rate=burn_rate,
        intent=float(rng.beta(*sim["intent_beta"])),
        annoyance=0.0,
        hardship=bool(rng.random() < sim["hardship_p"]),
        dispute_prone=bool(rng.random() < sim["dispute_prone_p"]),
        mandate_defect=None,                  # set from the defect mix
        responsiveness=float(rng.beta(*sim["responsiveness_beta"])),
    )


def burn_in(latent: LatentAccount, rng: np.random.Generator, sim: Mapping[str, Any],
            start: date, days: int = 45) -> None:
    """Run the balance forward before the batch starts, so day 0 balances sit at a
    realistic point in the sawtooth rather than all at the same phase."""
    from datetime import timedelta
    latent.balance_paise = int(latent.inflow_paise * rng.uniform(0.1, 0.9))
    for i in range(days):
        latent.tick(start - timedelta(days=days - i), rng, sim)


def apply_contact(latent: LatentAccount, channel: Channel, lifts_intent: bool,
                  sim: Mapping[str, Any]) -> None:
    """Contact moves intent and annoyance. It does not move balance. Ever.

    `lifts_intent` is False for a bare pre-debit notice, which informs but does not ask.
    """
    latent.annoyance = min(1.0, latent.annoyance + sim["annoy"][channel.value])
    if lifts_intent:
        lift = sim["intent_lift"][channel.value]
        latent.intent = min(1.0, latent.intent
                            + lift
                            * (1 - latent.intent)      # diminishing returns
                            * (1 - latent.annoyance))  # annoyed => no lift
    # balance_paise is untouched. This line is the thesis.


def selfpay_hazard(latent: LatentAccount, amount_paise: int, notified_recently: bool,
                   sim: Mapping[str, Any]) -> float:
    """Daily probability the customer pays through some other route.

    Note the `notified_mult` term: the pre-debit notice is *legally mandatory*, and it
    lifts self-pay on its own. Some of what a naive system credits to clever messaging is
    caused by the notification the regulator forced it to send. The evaluator reports
    that separately as notice-attributable lift.
    """
    s = sim["selfpay"]
    funds = 1.0 if latent.balance_paise >= amount_paise else s["no_funds_factor"]
    return (s["base"]
            * (1 + s["intent_mult"] * latent.intent)
            * (1 + s["notified_mult"] * notified_recently)
            * funds
            * max(0.0, 1 - s["annoyance_damp"] * latent.annoyance))


def contact_hazards(latent: LatentAccount, sim: Mapping[str, Any]) -> dict[str, float]:
    """Opt-out, complaint and dispute probabilities for one contact.

    The quadratic annoyance term is what punishes hammering: three contacts is cheap,
    eight is expensive. It is what makes the lambda frontier bend.
    """
    h = sim["hazards"]
    a = latent.annoyance
    return {
        "opt_out": h["opt_out"]["base"] + h["opt_out"]["annoy_sq"] * a * a,
        "complaint": h["complaint"]["base"] + h["complaint"]["annoy_sq"] * a * a,
        "dispute": h["dispute"]["prone"] * latent.dispute_prone + h["dispute"]["annoy"] * a,
        "mandate_cancel": h["mandate_cancel"]["base"] + h["mandate_cancel"]["annoy_sq"] * a * a,
    }
