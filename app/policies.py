"""The baseline policies. docs/06-PLANNER.md §Baseline policies.

The margin over these is a more credible claim than a raw recovery rate, so all of them
ship and all of them run through the same executor, the same gate and the same ledger.
A policy decides *what to attempt*; whether it is permitted is never its business.

    nothing   merchant default: notice + retry on day +3 and +7. The floor, and the
              behaviour the holdout arm receives whatever policy the treatment arm runs.
    fixed     the realistic incumbent: notice + retry on +1/+3/+7/+14, one SMS on +2.
    oracle    the ceiling. Given the customer's actual balance. A diagnostic, never a
              reported result — see app/sim/oracle.py.
    agent     the planner. M8.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Protocol

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import ActionType, Channel


class PolicyFn(Protocol):
    """A policy acts through the executor, so every action it takes is gated and logged.
    It cannot reach a rail directly, and it never sees a `GateDecision`."""

    def __call__(self, ex, world, account_id: str, day: date, offset: int) -> None: ...


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=IST)


def merchant_default(ex, world, account_id: str, day: date, offset: int) -> None:
    """A pre-debit notice plus retries on day +3 and +7, and nothing else.

    This is not "nothing at all": the notice is legally mandatory and cannot be withheld
    to make a control group look worse. It is also the *only* thing the holdout arm ever
    receives, which is what makes the comparison a comparison of two policies.
    """
    _schedule(ex, world, account_id, day, offset,
              retry_days=list(ex.cfg.baselines["merchant_default_retry_days"]))


def fixed_schedule(ex, world, account_id: str, day: date, offset: int) -> None:
    """The incumbent: more attempts, on a calendar, plus a nudge. No idea why any of them
    failed and no idea when the customer's money arrives."""
    cfg = ex.cfg
    retry_days = list(cfg.baselines["fixed_retry_days"])
    _schedule(ex, world, account_id, day, offset, retry_days=retry_days)
    if offset == int(cfg.baselines["fixed_sms_day"]):
        ex.message(account_id, _at(day, time(11, 0)), Channel.SMS,
                   "DLT_RECOVERY_REMIND_001")


def _schedule(ex, world, account_id: str, day: date, offset: int,
              retry_days: list[int]) -> None:
    """Notice the day before each scheduled retry, then the retry. The 24-hour window,
    exactly — which is what makes an attempt a commitment made a day in advance."""
    mandate = world.primary_mandate(account_id)
    if mandate is None:
        return
    if offset + 1 in retry_days:
        ex.notice(account_id, mandate, _at(day, ex.notice_time),
                  _at(day + timedelta(days=1), ex.attempt_time))
    if offset in retry_days:
        ex.retry(account_id, mandate, _at(day, ex.attempt_time))


def agent_policy(ex, world, account_id: str, day: date, offset: int) -> None:
    """Diagnose, narrow to what is permissible, price it, act.

    Everything this reads is observable: the codes the rails returned, the notices we
    issued, the contacts we made, the dates past cycles collected on. It never touches
    latent state — `tests/test_boundaries.py` is the guard, and the inflow phase is
    inferred rather than looked up, which is the whole difference between this and the
    oracle.
    """
    from app.diagnose import (
        AccountHistory,
        eligible_actions,
        hardship_score,
        posterior,
        signals_from,
    )
    from app.plan import PlanState, days_until, estimate
    from app.plan.agent import choose
    from app.plan.mdp import PERIOD

    state = world.cycles[account_id]
    account = world.accounts[account_id]
    mandates = tuple(world.mandates.get(account_id, ()))
    cfg = ex.cfg
    at = _at(day, ex.attempt_time)

    history = AccountHistory(
        attempts=tuple(state.attempts),
        last_success_at=None,
        max_prior_success_paise=None,
        amount_paise=state.cycle.amount_paise,
        city_tier=account.city_tier,
        mandate_registered_at=mandates[0].registered_at if mandates else None,
        other_mandate_failing=False,
        # The merchant's own registry, which the rail did not write. This is what lets a
        # noisy decline code be overruled by a fact.
        mandate_status=mandates[0].status.value if mandates else None,
        mandate_cap_paise=mandates[0].cap_paise if mandates else None,
        afa_free_cap_paise=cfg.applicable_cap_paise(
            mandates[0].rail, account.merchant_category) if mandates else None)
    code = state.attempts[-1][1] if state.attempts else state.first_failure_code
    rail = mandates[0].rail if mandates else None
    post = posterior(code, rail, history, at, cfg, ex.codemap)
    afa = account.consent.afa_authorised_at
    afa_fresh = afa is not None and (at - afa).days <= int(
        cfg.raw["policy"]["afa_freshness_days"])
    from app.domain.ptp import is_open
    # Observable signals only. `latent.hardship` is what this is scored against, in the
    # evaluator — it is the answer, never an input.
    signals = signals_from(
        codes=tuple(c for _, c in state.attempts), codemap=ex.codemap,
        other_mandate_failing=state.other_mandate_failing,
        distress_language=state.distress_signalled,
        ptp_status=state.ptp.status if state.ptp else None)
    hardship = hardship_score(signals, cfg)
    eligible = eligible_actions(post, mandates, cfg, amount_paise=state.cycle.amount_paise,
                                category=account.merchant_category, afa_fresh=afa_fresh,
                                hardship_score=hardship,
                                ptp_open=is_open(state.ptp, day, cfg.policy.ptp_grace_days))

    inflow = estimate(state.prior_successes, cfg)
    healthy = [m for m in mandates if m.status.value == "ACTIVE"]
    plan_state = PlanState(
        days_left=max(0, cfg.horizon_days - offset),
        attempts_left=min(4, max(0, cfg.budgets.attempts_per_cycle
                                 - max(0, len(state.attempts) - 1))),
        contacts_left=min(3, max(0, cfg.budgets.contacts_per_week
                                 - len(state.recovery_contacts))),
        # Against the presentations this account would actually make, not against the
        # cycle total. A split's notices are issued per part, so asking about the total
        # answered "no notice" for ever: the agent re-noticed every day, `notice_pending`
        # never became true, and the debit the notices were for could never be reached.
        notice_pending=(world.rails.notices_for(
            healthy[0], ex._parts_for(account_id, healthy[0]), at) is not None
            if healthy else False),
        days_to_inflow=days_until(inflow, day, cfg) % PERIOD,
        mandate_ok=bool(healthy),
        alt_rail=bool(eligible.target_rails))

    # How many presentations a split would take here. The planner has to price the
    # action it would actually get: a two-part split and a three-part one differ in
    # fees, in harm, in attempts spent and in the chance every part clears.
    primary = world.primary_mandate(account_id)
    split_n = len(ex._parts_for(account_id, primary)) if primary else 1

    choice = choose(plan_state, post, eligible.actions, state.cycle.amount_paise,
                    inflow.concentration, cfg, ex.amount_scale, cfg.lambda_harm,
                    overlay=eligible.overlay, split_parts_n=max(2, split_n))

    # The proposer proposes; the planner is the fallback, never a retry loop. A proposal
    # outside the eligible set is discarded and the planner's choice stands — which is
    # what would have happened anyway, so falling through costs nothing.
    # Ask the proposer only where the planner has no clear winner. Confirming a decision
    # the arithmetic already settles costs a network round trip and changes nothing.
    margin = choice.is_close_call
    proposal = None
    consulted = (ex.proposer is not None and eligible.overlay != "hardship"
                 and margin is not None
                 and margin <= float(cfg.planner["proposer_margin"]))
    if consulted:
        proposal = ex.proposer.propose(
            plan_state, post, eligible.actions,
            amount_paise=state.cycle.amount_paise, inflow=inflow, mandates=mandates,
            merchant_category=account.merchant_category.value)
        if proposal is not None and proposal.action_type in eligible.actions:
            choice = replace(choice, action=proposal.action_type,
                             reason=f"proposed by {proposal.source}: {proposal.rationale}")

    ex.open_decision(account_id, at, post, eligible, choice, inflow, proposal,
                     hardship=(hardship, signals), consulted=consulted)
    _act(ex, world, account_id, day, choice, healthy, eligible)


def _act(ex, world, account_id: str, day: date, choice, healthy, eligible) -> None:
    """Deterministic code turns the chosen ActionType into a concrete action: the rail,
    the amount, the schedule, the template. The planner picks *what*; none of the
    particulars are its to decide."""
    from app.domain.enums import TerminalState

    a = choice.action
    if a is ActionType.WAIT:
        return
    if a is ActionType.CLOSE:
        ex.close(account_id, _at(day, ex.attempt_time),
                 choice.terminal or TerminalState.EV_BELOW_THRESHOLD, choice.reason)
        return
    if a is ActionType.SEND_PREDEBIT_NOTICE and healthy:
        # The notice describes tomorrow's debit, and whether that debit is one
        # presentation or several is fixed by the ceiling, not by tomorrow's decision —
        # so it is knowable today, and the notice can be right about it.
        ex.notice(account_id, healthy[0], _at(day, ex.notice_time),
                  _at(day + timedelta(days=1), ex.attempt_time),
                  split=len(ex._parts_for(account_id, healthy[0])) > 1)
        return
    if a is ActionType.RETRY_DEBIT and healthy:
        ex.retry(account_id, healthy[0], _at(day, ex.attempt_time))
        return
    if a is ActionType.SPLIT_DEBIT and healthy:
        ex.split_retry(account_id, healthy[0], _at(day, ex.attempt_time))
        return
    if a is ActionType.REREGISTER_MANDATE and eligible.target_rails:
        ex.reregister(account_id, sorted(eligible.target_rails, key=lambda r: r.value)[0],
                      _at(day, ex.attempt_time))
        return
    if a is ActionType.REQUEST_PTP:
        ex.request_ptp(account_id, _at(day, time(11, 0)), Channel.SMS)
        return
    if a is ActionType.VOICE_CONFIRM_PTP:
        ex.request_ptp(account_id, _at(day, time(11, 0)), Channel.VOICE,
                       action_type=ActionType.VOICE_CONFIRM_PTP)
        return
    if a in (ActionType.SEND_MESSAGE, ActionType.SEND_PAYMENT_LINK):
        ex.message(account_id, _at(day, time(11, 0)), Channel.SMS, TEMPLATES[a],
                   action_type=a)
        return
    if a in (ActionType.OFFER_ACCOMMODATION, ActionType.ESCALATE_HUMAN):
        ex.close(account_id, _at(day, ex.attempt_time),
                 TerminalState.HARDSHIP if a is ActionType.OFFER_ACCOMMODATION
                 else TerminalState.DISPUTED, f"routed out of automation by {a.value}")
        return
    # Falling off the end is how VOICE_CONFIRM_PTP became a silent no-op for eleven
    # milestones: the planner priced it, chose it, and nothing happened. Reaching here
    # now means either a new ActionType with no execution path, or a debit/notice/
    # re-registration selected with no mandate to run it against.
    ex.unexecutable(account_id, _at(day, ex.attempt_time), a,
                    "no healthy mandate" if not healthy else "no execution path")


#: DLT-registered templates, one per customer-facing action. The agent selects an action;
#: which words the customer sees was decided at registration time, not by the agent.
TEMPLATES = {
    ActionType.SEND_MESSAGE: "DLT_RECOVERY_FIXIT_001",
    ActionType.SEND_PAYMENT_LINK: "DLT_RECOVERY_LINK_001",
    ActionType.REQUEST_PTP: "DLT_RECOVERY_PTP_001",
}


def build(name: str, cfg: Config) -> PolicyFn:
    if name == "nothing":
        return merchant_default
    if name == "fixed":
        return fixed_schedule
    if name == "oracle":
        from app.sim.oracle import oracle_policy
        return oracle_policy
    if name == "agent":
        return agent_policy
    raise NotImplementedError(
        f"policy '{name}' is not built yet — see docs/10-BUILD-PLAN.md")


IMPLEMENTED_NAMES = ("nothing", "fixed", "oracle", "agent")
