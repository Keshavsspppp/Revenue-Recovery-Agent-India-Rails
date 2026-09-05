"""M8 acceptance: the planner.

The tests that matter here are about *shape*, not level: does the agent wait before
payday, does it repair rather than retry a dead mandate, is a debit reachable only
through a notice, and does effort scale with the size of the ticket. A planner that
recovers more money by doing the wrong things for the wrong reasons is not what this
project is for.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pytest

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, TerminalState
from app.domain.models import InflowEstimate
from app.plan import PlanState, days_since, days_until, estimate, p_funds, solve
from app.plan.agent import choose
from app.plan.mdp import ACTIONS, PERIOD, q_at

MEDIAN = 120_000          # a representative batch median, in paise
SMALL = 9_900             # Rs 99
MID = 149_900             # Rs 1,499
LARGE = 4_200_000         # Rs 42,000


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


def sol(cause: CauseClass, amount: int, cfg: Config, conc: float = 0.8,
        lam: float | None = None):
    return solve(cause, amount, conc, cfg, lambda_harm=lam, amount_scale=MEDIAN)


def best(cause: CauseClass, amount: int, st: PlanState, cfg: Config,
         conc: float = 0.8, lam: float | None = None) -> ActionType:
    s = sol(cause, amount, cfg, conc, lam)
    qs = {a: q for a, q in q_at(s, st, cfg).items() if q > -1e17}
    return max(qs, key=qs.get)


# ---- inflow estimation ------------------------------------------------------------

def test_circular_mean_handles_the_month_boundary(cfg):
    """Day 30 and day 1 are two days apart, not twenty-nine. An arithmetic mean over
    {29, 30, 1, 2} gives day 15 — the middle of the month, which is exactly wrong and
    wrong in a way that looks perfectly plausible in a table."""
    days = [date(2026, m, d) for m, d in ((5, 29), (6, 30), (7, 1), (8, 2))]
    est = estimate(days, cfg)
    assert est.day_of_month in (30, 1) or est.day_of_month <= 2 or est.day_of_month >= 29
    assert est.concentration > 0.9


def test_concentration_reflects_regularity(cfg):
    tight = estimate([date(2026, m, 5) for m in (4, 5, 6, 7)], cfg)
    scattered = estimate([date(2026, 4, 2), date(2026, 5, 14),
                          date(2026, 6, 26), date(2026, 7, 9)], cfg)
    assert tight.concentration > 0.95
    assert scattered.concentration < 0.4


def test_cold_start_falls_back_to_the_population_prior(cfg):
    """Cold-start accounts are a real weakness. The fallback is declared and its
    confidence is set low, so the planner treats the estimate as the guess it is."""
    est = estimate([], cfg)
    assert est.n_observations == 0
    assert est.concentration == cfg.planner["inflow_fallback_concentration"]
    assert 1 <= est.day_of_month <= 28
    assert estimate([date(2026, 5, 3)], cfg).n_observations == 1


def test_days_until_and_since_are_complementary(cfg):
    est = InflowEstimate(day_of_month=10, concentration=0.9, n_observations=5)
    for day in (date(2026, 9, d) for d in (1, 9, 10, 11, 25)):
        assert (days_until(est, day, cfg) + days_since(est, day, cfg)) % PERIOD == 0


def test_the_estimate_uses_only_observable_history():
    """Structural: the module must not be able to see the simulator's salary date."""
    import inspect
    import app.plan.inflow as mod
    code = "\n".join(l.split("#")[0] for l in inspect.getsource(mod).splitlines())
    assert "app.sim" not in code
    assert "latent" not in code
    assert ".inflow_day" not in code      # the attribute, not the config key


# ---- the success model -------------------------------------------------------------

def test_p_funds_peaks_after_inflow_and_decays(cfg):
    curve = [float(p_funds(d, 0.8, cfg)) for d in range(0, 25)]
    assert curve[0] > 0.9, "money is there the day it lands"
    assert curve[0] > curve[10] > curve[20], "and burns down from there"
    assert curve[20] < 0.1


def test_confidence_raises_the_curve(cfg):
    assert p_funds(5, 0.9, cfg) > p_funds(5, 0.1, cfg)


# ---- the acceptance cases ----------------------------------------------------------

def test_planner_waits(cfg):
    """INSUFFICIENT_FUNDS, inflow three days out, attempts available: WAIT — never an
    immediate debit. Spending a notice and an attempt before payday buys an option that
    expires worthless."""
    st = PlanState(days_left=20, attempts_left=4, contacts_left=3, notice_pending=False,
                   days_to_inflow=3, mandate_ok=True, alt_rail=True)
    assert best(CauseClass.INSUFFICIENT_FUNDS, MID, st, cfg) is ActionType.WAIT

    # even with a notice already live, it holds the option rather than burning it
    with_notice = PlanState(20, 4, 3, True, 3, True, True)
    assert best(CauseClass.INSUFFICIENT_FUNDS, MID, with_notice, cfg) \
        is not ActionType.RETRY_DEBIT


def test_planner_commits_a_day_before_the_money_lands(cfg):
    """The interesting decision in the whole system: buying the option to debit tomorrow,
    today, before you know whether the money arrived."""
    st = PlanState(20, 4, 3, False, 1, True, True)
    assert best(CauseClass.INSUFFICIENT_FUNDS, MID, st, cfg) \
        is ActionType.SEND_PREDEBIT_NOTICE


def test_planner_debits_when_the_money_is_there(cfg):
    st = PlanState(20, 4, 3, True, 0, True, True)
    assert best(CauseClass.INSUFFICIENT_FUNDS, MID, st, cfg) is ActionType.RETRY_DEBIT


def test_notice_coupling(cfg):
    """RETRY_DEBIT is unreachable from a state with no live notice — not discouraged,
    unreachable. It is priced at negative infinity, so no weighting can surface it."""
    s = sol(CauseClass.INSUFFICIENT_FUNDS, MID, cfg)
    for to_inflow in range(0, PERIOD, 3):
        for attempts in (1, 4):
            st = PlanState(20, attempts, 3, False, to_inflow, True, True)
            assert q_at(s, st, cfg)[ActionType.RETRY_DEBIT] < -1e17
            assert ACTIONS[int(s.pi[20][st.index()])] is not ActionType.RETRY_DEBIT


def test_a_debit_needs_a_mandate_and_a_budget(cfg):
    s = sol(CauseClass.INSUFFICIENT_FUNDS, MID, cfg)
    no_mandate = PlanState(20, 4, 3, True, 0, False, True)
    no_attempts = PlanState(20, 0, 3, True, 0, True, True)
    assert q_at(s, no_mandate, cfg)[ActionType.RETRY_DEBIT] < -1e17
    assert q_at(s, no_attempts, cfg)[ActionType.RETRY_DEBIT] < -1e17


def test_planner_repairs(cfg):
    """MANDATE_INVALID with an alternative rail: repair it, never retry it. No number of
    attempts fixes an NRE account, and the planner has to be able to see that repairing
    is worth paying for — which it can only do if the repaired state has retries in it."""
    st = PlanState(20, 4, 3, False, 5, False, True)
    s = sol(CauseClass.MANDATE_INVALID, MID, cfg)
    qs = q_at(s, st, cfg)
    assert qs[ActionType.RETRY_DEBIT] < -1e17
    assert qs[ActionType.REREGISTER_MANDATE] > 0, "repair must be worth paying for"

    repaired = PlanState(19, 4, 3, False, 4, True, False)
    assert s.value(repaired) > s.value(st), "a live mandate is worth more than a dead one"


def test_repair_is_not_offered_without_fresh_consent(cfg):
    """MANDATE_REVOKED: re-registering without fresh consent is explicitly wrong, and
    POL-AFA-002 refuses it at the gate. The planner must not price it either."""
    st = PlanState(20, 4, 3, False, 5, False, True)
    s = sol(CauseClass.MANDATE_REVOKED, MID, cfg)
    assert q_at(s, st, cfg)[ActionType.REREGISTER_MANDATE] < -1e17


def test_stopping_is_economic(cfg):
    """The stopping rule is a result, not a constant. Harm is priced against the batch
    median, so a fixed rupee cost per action is prohibitive on a small ticket and
    immaterial on a large one — and the agent stops chasing the first while still working
    the second. Nobody typed 'try four times' anywhere in this system."""
    st = PlanState(days_left=20, attempts_left=1, contacts_left=3, notice_pending=False,
                   days_to_inflow=3, mandate_ok=True, alt_rail=True)
    posterior = {CauseClass.INSUFFICIENT_FUNDS: 1.0}
    eligible = frozenset(ACTIONS)

    small = choose(st, posterior, eligible, SMALL, 0.8, cfg, MEDIAN)
    large = choose(st, posterior, eligible, LARGE, 0.8, cfg, MEDIAN)

    assert small.action is ActionType.CLOSE
    assert small.terminal is TerminalState.EV_BELOW_THRESHOLD
    assert "nothing now or later" in small.reason
    assert large.action is not ActionType.CLOSE
    assert large.terminal is None


def test_stopping_prices_the_chance_the_diagnosis_is_wrong(cfg):
    """Closing is irreversible and waiting is free, so the bar for giving up is the value
    of the *state* under the full posterior — not whether anything is worth doing today.

    Under the old rule, an account reported MANDATE_REVOKED that was really short of funds
    had every debit vetoed, nothing worth doing today, and was closed for good. Here the
    same state keeps a quarter of its belief on a recoverable cause, and that is enough.
    """
    st = PlanState(20, 4, 3, False, 2, True, True)
    eligible = frozenset(ACTIONS)
    mixed = {CauseClass.MANDATE_REVOKED: 0.75, CauseClass.INSUFFICIENT_FUNDS: 0.25}
    certain = {CauseClass.MANDATE_REVOKED: 1.0}

    assert choose(st, mixed, eligible, MID, 0.8, cfg, MEDIAN).action is not ActionType.CLOSE
    assert choose(st, certain, eligible, MID, 0.8, cfg, MEDIAN).action is ActionType.CLOSE


def test_effort_scales_with_the_ticket(cfg):
    """The same state, three amounts: the number of actions worth taking rises with what
    is at stake."""
    st = PlanState(20, 4, 3, False, 2, True, True)
    counts = []
    for amount in (SMALL, MID, LARGE):
        s = sol(CauseClass.INSUFFICIENT_FUNDS, amount, cfg)
        counts.append(sum(1 for q in q_at(s, st, cfg).values() if q > 0))
    assert counts[0] <= counts[1] <= counts[2], counts


def test_value_iteration_converges(cfg):
    """V finite and stable for every reachable state, and monotone in the horizon: more
    days cannot be worth less."""
    s = sol(CauseClass.INSUFFICIENT_FUNDS, MID, cfg)
    assert np.isfinite(s.V).all()
    assert (s.V >= 0).all(), "CLOSE is worth zero, so no state can price below it"
    for d in range(1, cfg.horizon_days + 1):
        assert (s.V[d] >= s.V[d - 1] - 1e-3).all(), f"V fell between {d-1} and {d}"


def test_value_rises_with_budget(cfg):
    s = sol(CauseClass.INSUFFICIENT_FUNDS, MID, cfg)
    base = PlanState(20, 1, 3, False, 2, True, True)
    richer = PlanState(20, 4, 3, False, 2, True, True)
    assert s.value(richer) >= s.value(base)


def test_lambda_monotone(cfg):
    """As the harm price rises, the agent takes fewer actions. If it does not, harm is
    not actually priced and the frontier plot would be nonsense."""
    st = PlanState(20, 4, 3, False, 2, True, True)
    worth_taking = []
    for lam in (0.0, 0.25, 0.5, 1.0, 2.0):
        s = sol(CauseClass.INSUFFICIENT_FUNDS, MID, Config.load(lambda_harm=lam),
                lam=lam)
        qs = q_at(s, st, Config.load(lambda_harm=lam))
        worth_taking.append(sum(1 for a, q in qs.items()
                                if q > 0 and a not in (ActionType.WAIT, ActionType.CLOSE)))
    assert worth_taking == sorted(worth_taking, reverse=True), worth_taking
    assert worth_taking[0] > worth_taking[-1], "lambda has no effect at all"


def test_the_discount_breaks_the_deferral_tie(cfg):
    """WAIT costs nothing, so without a time preference it ties every action it could
    postpone and the agent defers until the horizon runs out. Money today is worth more
    than the same money in three weeks, and the scoreboard already measures that."""
    s = sol(CauseClass.INSUFFICIENT_FUNDS, MID, cfg)
    st = PlanState(20, 4, 3, False, 1, True, True)
    qs = q_at(s, st, cfg)
    assert qs[ActionType.SEND_PREDEBIT_NOTICE] > qs[ActionType.WAIT]


# ---- the choice layer ---------------------------------------------------------------

def test_choice_respects_the_eligible_set(cfg):
    """The eligible set is the gate between diagnosis and proposal. The planner may only
    choose from what it produced, whatever the Q values say."""
    st = PlanState(20, 4, 3, True, 0, True, True)
    posterior = {CauseClass.INSUFFICIENT_FUNDS: 1.0}
    unrestricted = choose(st, posterior, frozenset(ACTIONS), MID, 0.8, cfg, MEDIAN)
    assert unrestricted.action is ActionType.RETRY_DEBIT

    narrowed = choose(st, posterior, frozenset({ActionType.WAIT, ActionType.CLOSE}),
                      MID, 0.8, cfg, MEDIAN)
    assert narrowed.action in (ActionType.WAIT, ActionType.CLOSE)


def test_choice_weights_by_the_posterior(cfg):
    """Solve per cause, then weight — which keeps Q inspectable per cause, and that is
    what the audit trail shows."""
    st = PlanState(20, 4, 3, True, 0, True, True)
    confident = choose(st, {CauseClass.INSUFFICIENT_FUNDS: 1.0}, frozenset(ACTIONS),
                       MID, 0.8, cfg, MEDIAN)
    split = choose(st, {CauseClass.INSUFFICIENT_FUNDS: 0.5,
                        CauseClass.ACCOUNT_TERMINAL: 0.5}, frozenset(ACTIONS),
                   MID, 0.8, cfg, MEDIAN)
    # Moving half the mass onto a cause that forbids debiting must cost the debit half
    # its weight — that is what "posterior-weighted" has to mean to be worth doing.
    assert (split.q_values[ActionType.RETRY_DEBIT]
            < confident.q_values[ActionType.RETRY_DEBIT])
    assert split.weighted_value <= confident.weighted_value


def test_choice_emits_reconstructable_evidence(cfg):
    st = PlanState(20, 4, 3, True, 0, True, True)
    c = choose(st, {CauseClass.INSUFFICIENT_FUNDS: 1.0}, frozenset(ACTIONS), MID, 0.8,
               cfg, MEDIAN)
    ev = c.evidence()
    assert ev and all(e.startswith("q:") and e.count(":") == 2 for e in ev)
    assert c.reason


def test_solutions_are_cached_not_resolved(cfg):
    """168 solves would otherwise happen per account per day. Same key, same object."""
    from app.plan.mdp import clear_cache, solution_for
    clear_cache()
    a = solution_for(CauseClass.INSUFFICIENT_FUNDS, MID, 0.8, cfg, amount_scale=MEDIAN)
    b = solution_for(CauseClass.INSUFFICIENT_FUNDS, MID, 0.8, cfg, amount_scale=MEDIAN)
    assert a is b


def test_the_planner_cannot_see_the_simulator():
    import ast
    from pathlib import Path
    for path in (Path(__file__).resolve().parents[1] / "app" / "plan").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.sim"), path
