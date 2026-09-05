"""One Config, loaded from config/default.yaml, hashed into the batch record.

CLAUDE.md rule 8: a number hardcoded in a module is a bug. Scalars used in control flow
get typed fields; the lookup tables (costs, harms, rail properties, sim parameters) stay
as mappings because that is genuinely what they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from app.domain.enums import ActionType, Channel, MerchantCategory, Rail
from app.domain.models import sha256_of

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


@dataclass(frozen=True)
class BudgetDefaults:
    attempts_per_cycle: int
    contacts_per_week: int
    voice_per_cycle: int
    spend_per_cycle_paise: int


@dataclass(frozen=True)
class PolicyConfig:
    quiet_start: str                  # "08:00" IST — POL-QH-001
    quiet_end: str                    # "19:00" IST
    voice_allow_holidays: bool
    bereavement_days: int
    max_contacts_per_day: int
    ptp_grace_days: int
    hardship_threshold: float


@dataclass(frozen=True)
class CircuitBreaker:
    opt_out_rate: float
    complaint_rate: float


@dataclass(frozen=True)
class Config:
    policy_version: str
    horizon_days: int
    holdout_frac: float
    lambda_harm: float
    budgets: BudgetDefaults
    policy: PolicyConfig
    circuit_breaker: CircuitBreaker
    rails: Mapping[str, Mapping[str, Any]]
    high_cap_categories: frozenset[str]
    action_costs: Mapping[str, Any]
    harm_weights: Mapping[str, Any]
    baselines: Mapping[str, Any]
    planner: Mapping[str, Any]
    sim: Mapping[str, Any]
    raw: Mapping[str, Any] = field(repr=False, default_factory=dict)

    # ---- loading -------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH, **overrides: Any) -> Config:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        run = raw["run"] | {k: v for k, v in overrides.items() if v is not None}
        cfg = cls(
            policy_version=raw["policy_version"],
            horizon_days=run["horizon_days"],
            holdout_frac=run["holdout_frac"],
            lambda_harm=run["lambda_harm"],
            budgets=BudgetDefaults(**raw["budgets"]),
            policy=PolicyConfig(
                quiet_start=raw["policy"]["quiet_hours"]["start"],
                quiet_end=raw["policy"]["quiet_hours"]["end"],
                voice_allow_holidays=raw["policy"]["voice_allow_holidays"],
                bereavement_days=raw["policy"]["bereavement_days"],
                max_contacts_per_day=raw["policy"]["max_contacts_per_day"],
                ptp_grace_days=raw["policy"]["ptp_grace_days"],
                hardship_threshold=raw["policy"]["hardship_threshold"],
            ),
            circuit_breaker=CircuitBreaker(**raw["circuit_breaker"]),
            rails=raw["rails"],
            high_cap_categories=frozenset(raw["high_cap_categories"]),
            action_costs=raw["action_costs"],
            harm_weights=raw["harm_weights"],
            baselines=raw["baselines"],
            planner=raw["planner"],
            sim=raw["sim"],
            raw=raw | {"run": run},
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """README invariant 5: an action with no cost and no harm weight cannot be
        scheduled, because the planner cannot price it. Fail at load, not at runtime."""
        for a in ActionType:
            for table, name in ((self.action_costs, "action_costs"),
                                (self.harm_weights, "harm_weights")):
                if a.value not in table:
                    raise ValueError(f"{name} is missing {a.value}")
        for r in Rail:
            if r.value not in self.rails:
                raise ValueError(f"rails config is missing {r.value}")
        for c in self.high_cap_categories:
            MerchantCategory(c)  # raises if the category name drifted

    @property
    def config_hash(self) -> str:
        return sha256_of(self.raw)

    @property
    def world_hash(self) -> str:
        """Everything that determines the *world*, which is everything except the harm
        price. lambda_harm changes what the agent chooses, never what the customers do —
        so sweeping it must not look like config drift, or the frontier cannot be run
        against a batch at all.
        """
        raw = {k: v for k, v in self.raw.items() if k != "run"}
        raw["run"] = {k: v for k, v in self.raw["run"].items() if k != "lambda_harm"}
        return sha256_of(raw)

    # ---- lookups used across modules ------------------------------------------

    def rail(self, rail: Rail) -> Mapping[str, Any]:
        return self.rails[rail.value]

    def attempt_fee_paise(self, rail: Rail) -> int:
        return int(self.rail(rail)["attempt_fee_paise"])

    def notice_hours(self, rail: Rail) -> int:
        return int(self.rail(rail)["notice_hours"])

    def applicable_cap_paise(self, rail: Rail, category: MerchantCategory) -> int | None:
        """The AFA-free ceiling for this rail and category. None = no cap concept
        (PAYMENT_LINK is customer-initiated, so AFA happens in the flow itself)."""
        r = self.rail(rail)
        key = "high_cap_paise" if category.value in self.high_cap_categories else "afa_free_cap_paise"
        cap = r.get(key)
        return None if cap is None else int(cap)

    def action_cost_paise(self, action_type: ActionType, channel: Channel | None = None,
                          rail: Rail | None = None) -> int:
        if action_type is ActionType.RETRY_DEBIT and rail is not None:
            return self.attempt_fee_paise(rail)
        entry = self.action_costs[action_type.value]
        return int(entry[channel.value] if isinstance(entry, dict) else entry)

    def harm_weight(self, action_type: ActionType, channel: Channel | None = None) -> float:
        entry = self.harm_weights[action_type.value]
        return float(entry[channel.value] if isinstance(entry, dict) else entry)
