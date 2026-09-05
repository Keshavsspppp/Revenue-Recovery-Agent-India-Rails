"""The success model, kept factorised on purpose.

    P_success(d) = P_funds(d) . P_infra . P_mandate

Each factor is separately explainable to someone who asks why the agent waited, which a
single fitted model is not. `P_mandate` is the one that makes `REREGISTER_MANDATE`
mechanically necessary rather than a nice idea: it is zero when no live mandate exists,
so no amount of retrying has any expected value at all.
"""

from __future__ import annotations

import numpy as np

from app.domain.config import Config
from app.domain.enums import ActionType


def sigmoid(x: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-x))


def p_funds(days_since_inflow: np.ndarray | int, concentration: float,
            cfg: Config) -> np.ndarray | float:
    """Probability the money is in the account.

    Rises sharply just after the estimated inflow day and decays as the month burns down.
    `concentration` scales the whole curve: an account whose past collections are tightly
    clustered gets a confident estimate; a scattered one gets a flatter, more cautious
    prediction, which is the right response to not knowing.
    """
    p = cfg.planner["p_funds"]
    clip = int(cfg.planner["delta_clip_days"])
    delta = np.clip(int(cfg.planner["peak_days"]) - np.asarray(days_since_inflow),
                    -clip, clip)
    return sigmoid(p["a"] + p["b"] * delta + p["c"] * concentration)


def p_infra(cfg: Config) -> float:
    """Flat, and deliberately so: the executor schedules debits at 10:00, which is
    off-peak by construction. Avoiding the 19:00-22:00 window costs nothing and is more
    reliable than predicting it."""
    return float(cfg.planner["p_infra_offpeak"])


def p_success(days_since_inflow: np.ndarray | int, concentration: float,
              mandate_ok: bool, cfg: Config) -> np.ndarray | float:
    if not mandate_ok:
        return np.zeros_like(np.asarray(days_since_inflow), dtype=float)
    return p_funds(days_since_inflow, concentration, cfg) * p_infra(cfg)


def p_recover(action: ActionType, days_since_inflow: np.ndarray | int,
              concentration: float, cfg: Config) -> np.ndarray | float:
    """Recovery probability for the non-debit actions.

    All of them route through the funds signal, because none of them can create money —
    they change the probability the customer pays through some other route, and only if
    the money is there to pay with. That constraint is the simulator's one rule, restated
    on the planner's side so the two agree.
    """
    lift = cfg.planner["lift"].get(action.value)
    if lift is None:
        return np.zeros_like(np.asarray(days_since_inflow), dtype=float)
    return float(lift) * p_funds(days_since_inflow, concentration, cfg)
