"""The budgeted MDP, solved exactly by backward induction. docs/06-PLANNER.md.

Because every retry is announced 24 hours ahead and consumes a customer contact, the
question is not "which day maximises P(success)". It is: given ~4 attempt slots, ~3
contact slots and a 30-day horizon, which *sequence* maximises expected recovered value
minus cost minus priced harm — including the option to spend nothing and wait.

That is a finite-horizon MDP with budgets, small enough to solve exactly. Exact beats
fitted here, and it is far easier to explain: every number in the answer is arithmetic
someone can redo.

Two things fall out of the formulation rather than being coded:

  * **Stopping.** If the best action prices out below zero, the answer is CLOSE with
    `EV_BELOW_THRESHOLD`. Nobody typed "try four times".
  * **Notice coupling.** `RETRY_DEBIT` is not directly reachable. It requires
    `notice_pending`, which only `SEND_PREDEBIT_NOTICE` sets and only from the day
    before — so committing to an attempt means committing a day in advance, before you
    know whether the money arrived. That is the interesting decision in the whole system.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass
from app.domain.money import AMOUNT_BANDS
from app.plan.success import p_funds, p_infra, p_recover

#: The state space. `voice_left` and `ptp_open` from docs/06 are deliberately absent: an
#: open promise collapses the eligible set to {WAIT} in the overlay before the planner is
#: consulted at all, and voice is M13. `fatigue_bucket` is dropped as redundant — it is a
#: function of `contacts_left`, and carrying both would double the state space to encode
#: the same fact twice.
MAX_ATTEMPTS = 5          # 0..4
MAX_CONTACTS = 4          # 0..3
PERIOD = 30               # days_to_inflow, 0..29

#: Actions the MDP prices. Ordered, and the index into the policy array.
ACTIONS: tuple[ActionType, ...] = (
    ActionType.WAIT,
    ActionType.SEND_PREDEBIT_NOTICE,
    ActionType.RETRY_DEBIT,
    ActionType.SEND_MESSAGE,
    ActionType.SEND_PAYMENT_LINK,
    ActionType.REQUEST_PTP,
    ActionType.REREGISTER_MANDATE,
    ActionType.SPLIT_DEBIT,
    ActionType.OFFER_ACCOMMODATION,
    ActionType.ESCALATE_HUMAN,
    ActionType.CLOSE,
)
ACTION_INDEX = {a: i for i, a in enumerate(ACTIONS)}

#: Actions that spend a discretionary customer contact. The pre-debit notice is absent
#: for the same reason POL-FREQ exempts it: a mandatory notification must not consume the
#: budget for recovery outreach.
CONTACT_SPENDING = frozenset({ActionType.SEND_MESSAGE, ActionType.SEND_PAYMENT_LINK,
                              ActionType.REQUEST_PTP})

#: The one cause whose prohibition on debiting is about mandate health and is *repairable*.
#: For it, `mandate_ok` is the honest encoding: retrying is forbidden while the mandate is
#: broken and permitted again once it has been moved onto a live rail — which is what
#: makes paying to repair worth anything.
#:
#: The others are deliberately absent. MANDATE_REVOKED and ACCOUNT_TERMINAL cannot be
#: repaired at all (one needs fresh consent, the other has no account behind it), so a
#: state where they hold a live mandate is unreachable — pricing retries in it would let
#: posterior mass on "this account is closed" silently support debiting it anyway.
#: LIMIT_EXCEEDED and AUTH_ARTEFACT are not mandate problems: retrying the same amount
#: over the same cap, or retrying silently while the customer still has to act, stays
#: wrong however healthy the mandate is.
MANDATE_HEALTH_CAUSES = frozenset({CauseClass.MANDATE_INVALID})


@dataclass(frozen=True)
class PlanState:
    days_left: int
    attempts_left: int
    contacts_left: int
    notice_pending: bool
    days_to_inflow: int
    mandate_ok: bool
    alt_rail: bool

    def index(self) -> tuple[int, int, int, int, int, int]:
        return (self.attempts_left, self.contacts_left, int(self.notice_pending),
                self.days_to_inflow, int(self.mandate_ok), int(self.alt_rail))


@dataclass
class Solution:
    """`V[d]` is the value of a state with `d` days left; `pi[d]` the action index."""

    V: np.ndarray            # float32: 168 cached solutions would be 200MB at float64
    pi: np.ndarray
    amount_paise: int
    cause: CauseClass
    lambda_harm: float
    concentration: float
    amount_scale: int
    eligible: frozenset            # the actions this solve priced; the rest are -inf
    #: Presentations a split would take at this amount and cap. Carried on the solution
    #: so `q_values` prices the split the same way the backward induction did — the two
    #: disagreeing is how an action gets chosen at a price nobody paid.
    split_parts_n: int = 2

    def best(self, state: PlanState) -> ActionType:
        return ACTIONS[int(self.pi[state.days_left][state.index()])]

    def value(self, state: PlanState) -> float:
        return float(self.V[state.days_left][state.index()])

    def q_values(self, state: PlanState, cfg: Config) -> dict[ActionType, float]:
        """Every action's price at this state, recomputed on demand rather than stored.

        This is what the audit trail shows when someone asks why the agent waited: not
        "the model said so", but Rs 54 of expected value against Rs 200 of attempt fee.
        """
        return q_at(self, state, cfg)


SHAPE = (MAX_ATTEMPTS, MAX_CONTACTS, 2, PERIOD, 2, 2)


def price(cfg: Config, a: ActionType) -> tuple[float, float]:
    """Cost and harm weight for an action, at the planner's declared channel.

    The planner chooses an ActionType; the executor fills the channel. Pricing both
    against `planner.default_channel` keeps the number the planner optimised against and
    the number the batch actually spends identical.
    """
    from app.domain.enums import Channel
    channel = Channel[cfg.planner["default_channel"]]
    return (float(cfg.action_cost_paise(a, channel)), cfg.harm_weight(a, channel))


def _grids() -> tuple[np.ndarray, ...]:
    return np.meshgrid(np.arange(MAX_ATTEMPTS), np.arange(MAX_CONTACTS), np.arange(2),
                       np.arange(PERIOD), np.arange(2), np.arange(2), indexing="ij")


def solve(cause: CauseClass, amount_paise: int, concentration: float, cfg: Config,
          lambda_harm: float | None = None, horizon: int | None = None,
          amount_scale: int | None = None, split_parts_n: int = 2) -> Solution:
    """Backward induction from the horizon. Exact, and it runs in milliseconds."""
    from app.diagnose.eligible import ALLOWED, UNIVERSAL, WRONG

    lam = cfg.lambda_harm if lambda_harm is None else lambda_harm
    H = cfg.horizon_days if horizon is None else horizon
    attempts, contacts, notice, to_inflow, mandate_ok, alt_rail = _grids()
    since_inflow = (PERIOD - to_inflow) % PERIOD

    funds = p_funds(since_inflow, concentration, cfg)
    infra = p_infra(cfg)
    # Harm is priced against the batch median, not this cycle's own value — which is what
    # makes lambda interpretable as a single dial across a heavy-tailed batch, and what
    # makes effort scale with the ticket. A fixed rupee cost per contact is prohibitive on
    # a Rs 299 subscription and immaterial on a Rs 42,000 EMI, so the agent stops chasing
    # the first and keeps working the second. Scaling by the account's own amount would
    # cancel exactly that out.
    amount_scale = amount_scale if amount_scale is not None else amount_paise

    eligible = ((ALLOWED.get(cause, frozenset()) - WRONG.get(cause, frozenset()))
                | UNIVERSAL)
    # Wherever a retry is worth pricing, so is the split — it is the same debit made
    # presentable under a ceiling, not a different remedy. The cause matrix decides
    # whether a *debit* makes sense here; the ceiling decides which shape it takes, and
    # that is not a fact about the cause.
    #
    # Without this the two eligible sets disagree: `eligible_actions` offers the split
    # for an over-cap account whose code says INSUFFICIENT_FUNDS, the solve never priced
    # it, `q_at` returns -inf, and the action is silently unreachable. Measured on one
    # 600-account batch: 74 accounts over the cap, zero splits chosen.
    if ActionType.RETRY_DEBIT in eligible:
        eligible = eligible | {ActionType.SPLIT_DEBIT}

    if cause in MANDATE_HEALTH_CAUSES:
        # These causes forbid a debit because the *mandate* is broken, and `mandate_ok`
        # already encodes that: `_q` refuses any debit while it is false. Excluding the
        # action from the whole solve instead would make repair pointless — the value of
        # moving to a live mandate is precisely the retries it unlocks, and a planner that
        # cannot see them will never pay to repair anything.
        eligible = eligible | {ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT,
                               ActionType.SEND_PREDEBIT_NOTICE}

    V = np.zeros((H + 1, *SHAPE), dtype=np.float32)
    pi = np.full((H + 1, *SHAPE), ACTION_INDEX[ActionType.CLOSE], dtype=np.int8)

    for d in range(1, H + 1):
        nxt = V[d - 1]
        Q = np.full((len(ACTIONS), *SHAPE), -np.inf)

        for a in ACTIONS:
            if a not in eligible and a not in UNIVERSAL:
                continue
            cost, harm_weight = price(cfg, a)
            harm = lam * harm_weight * amount_scale
            Q[ACTION_INDEX[a]] = _q(a, d, nxt, cost, harm, amount_paise, funds, infra,
                                    attempts, contacts, notice, to_inflow, mandate_ok,
                                    alt_rail, concentration, cfg,
                                    split_parts_n=split_parts_n)

        # CLOSE is always available and worth exactly zero: it is the floor that makes
        # the stopping rule economic. If every other action prices below it, stopping is
        # the optimal play rather than a hardcoded attempt count.
        Q[ACTION_INDEX[ActionType.CLOSE]] = 0.0
        V[d] = Q.max(axis=0)
        pi[d] = Q.argmax(axis=0).astype(np.int8)

    return Solution(V=V, pi=pi, amount_paise=amount_paise, cause=cause,
                    lambda_harm=lam, concentration=concentration,
                    amount_scale=amount_scale, eligible=frozenset(eligible),
                    split_parts_n=split_parts_n)


def _q(a: ActionType, d: int, nxt: np.ndarray, cost: float, harm: float,
       amount: int, funds: np.ndarray, infra: float, attempts: np.ndarray,
       contacts: np.ndarray, notice: np.ndarray, to_inflow: np.ndarray,
       mandate_ok: np.ndarray, alt_rail: np.ndarray, concentration: float,
       cfg: Config, split_parts_n: int = 2) -> np.ndarray:
    """Q(s, a) = p*amount - cost - lambda*harm*scale + gamma*(1-p)*V(s')."""
    tomorrow = (to_inflow - 1) % PERIOD          # one day closer to the next inflow
    forbidden = np.full(funds.shape, -np.inf)
    g = float(cfg.planner["daily_discount"])

    if a is ActionType.WAIT:
        # Recovers nothing today, but preserves every budget — and the value of that is
        # entirely in V(s'). An agent without this action cannot choose to do nothing.
        return g * nxt[attempts, contacts, notice, tomorrow, mandate_ok, alt_rail]

    if a is ActionType.SEND_PREDEBIT_NOTICE:
        # Buys the option to debit tomorrow. It does not spend a contact slot, and it
        # cannot be stacked: issuing a second notice while one is live is waste.
        v = g * nxt[attempts, contacts, 1, tomorrow, mandate_ok, alt_rail]
        return np.where((notice == 1) | (mandate_ok == 0) | (attempts == 0),
                        forbidden, v - cost - harm)

    if a in (ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT):
        # A split is n presentations of the same cycle, so everything scales with n: n
        # fees, n units of harm, n attempts out of the budget, and n independent chances
        # for the rail to fail. Collecting the whole amount needs all of them to clear.
        #
        # The parts sum to the cycle amount, so a split buys *nothing* against a balance
        # — needing the whole amount is needing the whole amount, and `funds` is unchanged.
        # What it buys is the per-transaction ceiling, and that shows up in eligibility
        # rather than here: over the cap, RETRY_DEBIT is not offered and this is.
        n = split_parts_n if a is ActionType.SPLIT_DEBIT else 1
        p = funds * infra ** n
        spent = np.maximum(attempts - n, 0)
        v_fail = nxt[spent, contacts, 0, tomorrow, mandate_ok, alt_rail]
        q = p * amount - cost * n - harm * n + g * (1 - p) * v_fail
        # The coupling: no live notice, no attempt. Nor without a mandate, nor without
        # enough of the attempt budget left for every part.
        return np.where((notice == 0) | (mandate_ok == 0) | (attempts < n),
                        forbidden, q)

    if a is ActionType.REREGISTER_MANDATE:
        # Repair onto another rail. The customer has to complete an authorisation, so it
        # only sometimes lands — but when it does, a dead account becomes debitable.
        p_afa = float(cfg.planner["reregister_afa_completion"])
        v_ok = nxt[attempts, contacts, notice, tomorrow, 1, 0]
        v_no = nxt[attempts, contacts, notice, tomorrow, mandate_ok, 0]
        q = g * (p_afa * v_ok + (1 - p_afa) * v_no) - cost - harm
        return np.where(alt_rail == 0, forbidden, q)

    if a in CONTACT_SPENDING:
        p = p_recover(a, (PERIOD - to_inflow) % PERIOD, concentration, cfg)
        spent = np.maximum(contacts - 1, 0)
        v_fail = nxt[attempts, spent, notice, tomorrow, mandate_ok, alt_rail]
        q = p * amount - cost - harm + g * (1 - p) * v_fail
        return np.where(contacts == 0, forbidden, q)

    if a in (ActionType.OFFER_ACCOMMODATION, ActionType.ESCALATE_HUMAN):
        p = p_recover(a, (PERIOD - to_inflow) % PERIOD, concentration, cfg)
        return p * amount - cost - harm          # terminal: no continuation value

    return forbidden


def q_at(sol: Solution, state: PlanState, cfg: Config) -> dict[ActionType, float]:
    """Q(s, .) for one state, using the very same `_q` the solver used.

    `_q` is written against numpy but every operation in it broadcasts over scalars, so
    the single-state path and the solve path cannot drift apart. Recomputing eleven
    numbers is cheaper than storing a Q table per cached solution.
    """
    if state.days_left <= 0:
        return {a: 0.0 for a in ACTIONS}
    nxt = sol.V[state.days_left - 1]
    since = (PERIOD - state.days_to_inflow) % PERIOD
    funds = p_funds(np.float64(since), sol.concentration, cfg)
    infra = p_infra(cfg)
    out: dict[ActionType, float] = {}
    for a in ACTIONS:
        if a not in sol.eligible:
            out[a] = float("-inf")     # not priced by this solve; not choosable
            continue
        cost, harm_weight = price(cfg, a)
        q = _q(a, state.days_left, nxt, cost,
               sol.lambda_harm * harm_weight * sol.amount_scale,
               sol.amount_paise, funds, infra,
               state.attempts_left, state.contacts_left, int(state.notice_pending),
               state.days_to_inflow, int(state.mandate_ok), int(state.alt_rail),
               sol.concentration, cfg, split_parts_n=sol.split_parts_n)
        out[a] = float(q)
    out[ActionType.CLOSE] = 0.0
    return out


@lru_cache(maxsize=256)
def _cached(cause: CauseClass, band_amount: int, conc_bucket: int, lam: float,
            horizon: int, amount_scale: int, config_hash: str,
            split_parts_n: int) -> Solution:
    from app.domain.config import Config as _C
    cfg = _C.load()
    conc = (conc_bucket + 0.5) / int(cfg.planner["concentration_buckets"])
    return solve(cause, band_amount, conc, cfg, lambda_harm=lam, horizon=horizon,
                 amount_scale=amount_scale, split_parts_n=split_parts_n)


def solution_for(cause: CauseClass, amount_paise: int, concentration: float,
                 cfg: Config, lambda_harm: float | None = None,
                 amount_scale: int | None = None, split_parts_n: int = 2) -> Solution:
    """Cached per (cause, amount band, concentration bucket).

    The value function depends on the amount through the cost/amount ratio, which is
    exactly what makes stopping economic: a Rs 299 subscription and a Rs 42,000 EMI get
    different answers from the same state. Banding keeps that without solving per account.
    """
    lam = cfg.lambda_harm if lambda_harm is None else lambda_harm
    buckets = int(cfg.planner["concentration_buckets"])
    bucket = min(buckets - 1, int(concentration * buckets))
    return _cached(cause, _band_amount(amount_paise), bucket, float(lam),
                   cfg.horizon_days, int(amount_scale), cfg.world_hash,
                   int(split_parts_n))


def _band_amount(paise: int) -> int:
    """A representative amount for the band: the geometric midpoint, so the ratio of cost
    to amount — the thing the value function actually turns on — is right in the middle."""
    rupees = paise / 100
    lo = 0.0
    for hi, _ in AMOUNT_BANDS:
        if rupees < hi:
            return int(((max(lo, 1) * hi) ** 0.5) * 100)
        lo = hi
    return int(75_000 * 100)


def clear_cache() -> None:
    _cached.cache_clear()
