"""The planner: a budgeted MDP over a closed action set, solved by backward induction.

Nothing here may import `app.sim`. The inflow phase is inferred from observable history
and nothing else, which is the difference between a result and a rehearsal.
"""

from app.plan.inflow import days_since, days_until, estimate
from app.plan.mdp import (
    ACTIONS,
    PERIOD,
    PlanState,
    Solution,
    clear_cache,
    solution_for,
    solve,
)
from app.plan.success import p_funds, p_infra, p_recover, p_success

__all__ = ["ACTIONS", "PERIOD", "PlanState", "Solution", "clear_cache", "days_since",
           "days_until", "estimate", "p_funds", "p_infra", "p_recover", "p_success",
           "solution_for", "solve"]
