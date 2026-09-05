"""Layer 2: a posterior over cause classes.

True cause is latent. Layer 1 gives one noisy observation of it — and the codes are known
to drift, since NPCI's own rewrite added 20, revised 33 and removed 22. Layer 2 combines
that observation with the account's history.

This is a hand-written Bayesian update rather than a fitted model: prior from the code
map, one likelihood ratio per observed feature, normalise. `docs/04-CAUSE-TAXONOMY.md`
offers that as an alternative to multinomial logistic regression and asks which was chosen
and why — the reasoning is in docs/DECISIONS.md, but the short version is that every
number is a named, inspectable claim, which is what the audit trail has to show.

Every feature here is observable. Nothing in this module may see the simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from app.domain.codemap import CodeMap
from app.domain.config import Config
from app.domain.enums import CauseClass, Rail


@dataclass(frozen=True)
class AccountHistory:
    """Observable history. Assembled from the ledger, never from the world."""

    attempts: tuple[tuple[datetime, str], ...] = ()   # (when, rail code)
    last_success_at: datetime | None = None
    max_prior_success_paise: int | None = None
    amount_paise: int = 0
    city_tier: int = 1
    mandate_registered_at: datetime | None = None
    other_mandate_failing: bool = False
    #: The merchant's own record of the mandate, which the rail did not write. A decline
    #: code claiming the mandate is dead while the registry says ACTIVE is a code
    #: disagreeing with a fact, and the fact is the more reliable of the two.
    mandate_status: str | None = None
    mandate_cap_paise: int | None = None
    afa_free_cap_paise: int | None = None

    @property
    def attempts_this_cycle(self) -> int:
        return len(self.attempts)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(code for _, code in self.attempts)


def code_prior(code: str, rail: Rail, codemap: CodeMap,
               weight: float) -> dict[CauseClass, float]:
    """Layer 1 as a soft prior. The mapped class gets `weight`; the rest is spread evenly,
    because the map is evidence about a drifting taxonomy rather than ground truth."""
    causes = [c for c in CauseClass]
    mapped = codemap.cause_of(code)
    if codemap.is_unmapped(code):
        # An unmapped code tells you the taxonomy moved, not what went wrong. Spread the
        # mass and let the history features do the work.
        others = [c for c in causes if c is not CauseClass.UNKNOWN]
        return {**{c: (1 - 0.30) / len(others) for c in others},
                CauseClass.UNKNOWN: 0.30}
    rest = (1.0 - weight) / (len(causes) - 1)
    return {c: (weight if c is mapped else rest) for c in causes}


#: Causes whose claim the merchant's own records can contradict.
MANDATE_DEATH = frozenset({CauseClass.MANDATE_INVALID, CauseClass.MANDATE_REVOKED,
                           CauseClass.ACCOUNT_TERMINAL})


def contradictions(claimed: CauseClass, history: AccountHistory) -> tuple[str, ...]:
    """Where the reported code disagrees with what we already know.

    This is the only feature in the set strong enough to overturn the code prior, and it
    earns that by not coming from the rail. A mandate's lifecycle state is written by
    registration and revocation events; the cap is what we registered. Neither is a
    per-transaction decline code, so when they disagree the code is the weaker witness.
    """
    fired: list[str] = []
    active = history.mandate_status == "ACTIVE"
    if claimed in MANDATE_DEATH and active:
        fired.append("records_contradict_mandate_death")
    if claimed is CauseClass.LIMIT_EXCEEDED:
        within_mandate = (history.mandate_cap_paise is None
                          or history.amount_paise <= history.mandate_cap_paise)
        within_afa = (history.afa_free_cap_paise is None
                      or history.amount_paise <= history.afa_free_cap_paise)
        if within_mandate and within_afa:
            fired.append("records_contradict_limit")
    if claimed is CauseClass.AUTH_ARTEFACT and active:
        fired.append("records_contradict_auth")
    return tuple(fired)


def observed_features(history: AccountHistory, at: datetime,
                      cfg: Config) -> tuple[str, ...]:
    """Which evidence keys fire for this account, right now. Named so the ledger can carry
    them and a human can re-derive the posterior by hand."""
    d = cfg.raw["diagnose"]
    peak_lo, peak_hi = cfg.sim["peak_hours"]
    codes = history.codes
    fired: list[str] = []

    if len(codes) >= 2 and codes[-1] == codes[-2]:
        fired.append("repeated_same_code")
    if len(codes) >= 2 and codes[-1] != codes[-2]:
        fired.append("code_changed_between_attempts")
    if peak_lo <= at.hour < peak_hi:
        fired.append("peak_hour")
    if (history.max_prior_success_paise is not None
            and history.amount_paise > history.max_prior_success_paise):
        fired.append("amount_above_prior_max")
    if history.city_tier == 3:
        fired.append("tier3")
    if history.other_mandate_failing:
        fired.append("other_mandate_failing")
    if (history.mandate_registered_at is not None
            and at - history.mandate_registered_at
            <= timedelta(days=int(d["recent_registration_days"]))):
        fired.append("recent_mandate_registration")
    if (history.last_success_at is None
            or at - history.last_success_at
            > timedelta(days=int(d["long_since_success_days"]))):
        fired.append("long_since_success")
    return tuple(fired)


def posterior(code: str, rail: Rail, history: AccountHistory, at: datetime,
              cfg: Config, codemap: CodeMap) -> dict[CauseClass, float]:
    """P(cause | code, history). Pure: same inputs, same distribution."""
    d = cfg.raw["diagnose"]
    weights: Mapping[str, Mapping[str, float]] = d["evidence"]

    dist = code_prior(code, rail, codemap, float(d["code_prior_weight"]))
    features = observed_features(history, at, cfg) + contradictions(
        codemap.cause_of(code), history)
    for feature in features:
        for cause_name, ratio in weights.get(feature, {}).items():
            dist[CauseClass(cause_name)] *= float(ratio)

    total = sum(dist.values())
    if total <= 0:
        return {c: 1.0 / len(CauseClass) for c in CauseClass}
    return {c: v / total for c, v in dist.items()}


def top(dist: dict[CauseClass, float]) -> CauseClass:
    return max(dist, key=dist.get)


def as_evidence(code: str, history: AccountHistory, at: datetime,
                cfg: Config) -> tuple[str, ...]:
    """Evidence strings for the DIAGNOSE event — each a code, a count or a bucket."""
    from app.domain.codemap import load_codemap
    claimed = load_codemap().cause_of(code)
    return (f"rail_code:{code}",
            f"attempts_made:{history.attempts_this_cycle}",
            f"city_tier:{history.city_tier}",
            f"mandate_status:{history.mandate_status or 'unknown'}",
            *(f"feature:{f}" for f in observed_features(history, at, cfg)),
            *(f"contradiction:{f}" for f in contradictions(claimed, history)))
