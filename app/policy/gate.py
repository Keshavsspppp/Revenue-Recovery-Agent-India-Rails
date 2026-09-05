"""The compliance gate: one pure function every action passes through.

    evaluate(action, ctx, policy) -> GateDecision

No I/O, no clock reads, no randomness. `ctx.now` is the *simulated* clock in IST, passed
in by the caller, so the same inputs always produce the same verdict — which is what makes
a denial reproducible months later when both the policy and the model have changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from app.domain.config import Config
from app.domain.enums import Rail, Verdict
from app.domain.models import (
    Account,
    Action,
    BillingCycle,
    Budgets,
    ConsentState,
    GateDecision,
    Mandate,
    NoticeReceipt,
    PromiseToPay,
)
from app.policy.rules import RULES, Rule, rules_hash


@dataclass(frozen=True)
class Calendar:
    """Festival and gazetted-holiday dates.

    `regional` is declared and unused: `Account` carries no region field, so POL-QH-003
    is enforced against the national list only. Narrowing it per region needs a region on
    the account, not a change here. See docs/DECISIONS.md.
    """

    festivals: frozenset[date] = frozenset()
    holidays: frozenset[date] = frozenset()
    regional: Mapping[str, frozenset[date]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Config) -> Calendar:
        p = cfg.raw["policy"]
        return cls(
            festivals=frozenset(date.fromisoformat(d) for d in p.get("festivals_2026", ())),
            holidays=frozenset(date.fromisoformat(d)
                               for d in p.get("gazetted_holidays_2026", ())))


@dataclass(frozen=True)
class AccountFlags:
    """Conduct flags. Observable state, never latent truth."""

    disputed: bool = False
    subjudice: bool = False
    bereavement_at: datetime | None = None
    escalated_this_cycle: bool = False
    terminal_state: str | None = None


@dataclass(frozen=True)
class GateContext:
    """Everything the rules may look at. Assembled by the caller; the gate reads only."""

    now: datetime                       # simulated, IST
    account: Account
    consent: ConsentState
    mandates: tuple[Mandate, ...]
    cycle: BillingCycle
    budgets: Budgets
    contacts_made: tuple[datetime, ...]
    notices: tuple[NoticeReceipt, ...]
    ptp: PromiseToPay | None
    flags: AccountFlags
    calendar: Calendar
    cfg: Config

    # ---- config projections, so rules stay one-liners ------------------------

    @property
    def quiet_start(self) -> int:
        return int(self.cfg.policy.quiet_start.split(":")[0])

    @property
    def quiet_end(self) -> int:
        return int(self.cfg.policy.quiet_end.split(":")[0])

    @property
    def voice_allow_holidays(self) -> bool:
        return self.cfg.policy.voice_allow_holidays

    @property
    def bereavement_days(self) -> int:
        return self.cfg.policy.bereavement_days

    @property
    def max_contacts_per_week(self) -> int:
        return self.cfg.budgets.contacts_per_week

    @property
    def max_contacts_per_day(self) -> int:
        return self.cfg.policy.max_contacts_per_day

    @property
    def ptp_grace_days(self) -> int:
        return self.cfg.policy.ptp_grace_days

    @property
    def afa_freshness_days(self) -> int:
        return int(self.cfg.raw["policy"]["afa_freshness_days"])

    @property
    def dlt_templates(self) -> Mapping[str, str]:
        return self.cfg.raw["policy"]["dlt_templates"]

    @property
    def cli_series_service(self) -> str:
        return self.cfg.raw["policy"]["cli_series_service"]

    @property
    def cli_series_promotional(self) -> str:
        return self.cfg.raw["policy"]["cli_series_promotional"]

    @property
    def notice_hours(self) -> int:
        rails = [m.rail for m in self.mandates] or [Rail.ENACH]
        return max(self.cfg.notice_hours(r) for r in rails)

    @property
    def afa_free_cap(self) -> int | None:
        """The ceiling for this rail and merchant category. Insurance, mutual funds and
        credit-card bills get the higher one."""
        rail = self.mandates[0].rail if self.mandates else Rail.ENACH
        return self.cfg.applicable_cap_paise(rail, self.account.merchant_category)

    def action_cost(self, action: Action) -> int:
        return self.cfg.action_cost_paise(action.type, action.channel, action.rail)


@dataclass(frozen=True)
class PolicySet:
    """A versioned, ordered rule set. Changing any rule changes `rules_hash`."""

    version: str
    rules: tuple[Rule, ...] = RULES

    @property
    def hash(self) -> str:
        return rules_hash(self.rules)

    @classmethod
    def from_config(cls, cfg: Config) -> PolicySet:
        return cls(version=cfg.policy_version)


def evaluate(action: Action, ctx: GateContext, policy: PolicySet,
             decision_id: str = "dec_unassigned") -> GateDecision:
    """Evaluate every applicable rule in order. The first DENY short-circuits, and every
    rule checked before it is recorded — so the trail shows what was examined, not just
    what failed."""
    passed: list[str] = []
    for rule in policy.rules:
        if action.type not in rule.applies_to:
            continue
        if rule.check(action, ctx):
            passed.append(rule.rule_id)
            continue
        return GateDecision(
            decision_id=decision_id, action_hash=action.hash(), verdict=Verdict.DENY,
            rule_ids_passed=tuple(passed), rule_id_failed=rule.rule_id,
            reason=rule.deny_reason, basis=rule.basis,
            policy_version=policy.version, evaluated_at=ctx.now)
    return GateDecision(
        decision_id=decision_id, action_hash=action.hash(), verdict=Verdict.ALLOW,
        rule_ids_passed=tuple(passed), rule_id_failed=None, reason=None, basis=None,
        policy_version=policy.version, evaluated_at=ctx.now)


def catalogue() -> list[dict[str, Any]]:
    """The rule catalogue, for GET /policy/rules."""
    return [{"rule_id": r.rule_id, "title": r.title, "basis": r.basis,
             "applies_to": sorted(a.value for a in r.applies_to)} for r in RULES]
