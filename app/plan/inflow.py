"""Inflow phase estimation from observable history.

The planner's most valuable single input, and it comes from the timestamps of the
account's past *successful* debits — nothing else. Never the simulator's salary date;
`tests/test_boundaries.py` enforces that structurally.

The mean is circular because day 30 and day 1 are two days apart, not twenty-nine. An
arithmetic mean over {29, 30, 1, 2} gives day 15 — the middle of the month, which is
exactly wrong, and wrong in a way that looks plausible in a table.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Sequence

from app.domain.config import Config
from app.domain.models import InflowEstimate


def estimate(success_days: Sequence[date], cfg: Config) -> InflowEstimate:
    """Circular mean and concentration over the days of month money arrived.

    With fewer than two observations there is nothing to average, so fall back to the
    population prior. How often that fires is reported: cold-start accounts are a real
    weakness of this approach and naming it is better than hiding it.
    """
    period = int(cfg.planner["inflow_period_days"])
    days = [d.day for d in success_days]
    if len(days) < 2:
        return InflowEstimate(
            day_of_month=_population_prior(cfg),
            concentration=float(cfg.planner["inflow_fallback_concentration"]),
            n_observations=len(days))

    angles = [2 * math.pi * (d % period) / period for d in days]
    x = sum(math.cos(a) for a in angles) / len(angles)
    y = sum(math.sin(a) for a in angles) / len(angles)
    day = round(period * math.atan2(y, x) / (2 * math.pi)) % period
    concentration = math.sqrt(x * x + y * y)      # 0 = scattered, 1 = all on one day
    return InflowEstimate(day_of_month=day or period, concentration=concentration,
                          n_observations=len(days))


def _population_prior(cfg: Config) -> int:
    """Start-of-month heavy, per the generative model in docs/03-SIMULATOR.md. The
    weighted mode of the declared mixture, not a number invented here."""
    mixture = cfg.sim["inflow_day_mixture"]
    weight, lo, hi = max(mixture, key=lambda m: m[0])
    return int((lo + hi) // 2)


def days_until(estimate: InflowEstimate, today: date, cfg: Config) -> int:
    """Days from `today` until the next estimated inflow, 0..period-1."""
    period = int(cfg.planner["inflow_period_days"])
    if estimate.day_of_month is None:
        return period // 2
    return (estimate.day_of_month - today.day) % period


def days_since(estimate: InflowEstimate, today: date, cfg: Config) -> int:
    """Days since the last estimated inflow. This is what drives the funds model: a
    balance is highest just after the money lands and burns down from there."""
    period = int(cfg.planner["inflow_period_days"])
    return (period - days_until(estimate, today, cfg)) % period
