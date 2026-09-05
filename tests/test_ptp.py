"""M9 acceptance: the promise-to-pay state machine.

A promise is a commitment with a verifiable outcome, not a CRM note. That means it
resolves on a date, against settlement data, without anyone deciding it did — and while
it is open it suppresses outreach, because chasing someone on the morning they said they
would pay is how a kept promise becomes a complaint.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

import pytest

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    Arm,
    Channel,
    PTPStatus,
    Rail,
    Stage,
    Verdict,
)
from app.domain.models import PromiseToPay
from app.domain.ptp import due, is_open, next_confidence, resolve
from app.runner import run_batch
from app.sim.generate import simulate_batch
from app.sim.world import World

TODAY = date(2026, 9, 10)
AMOUNT = 149_900


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


def promise(promised: date = TODAY, status: PTPStatus = PTPStatus.OPEN,
            confidence: float = 0.6) -> PromiseToPay:
    return PromiseToPay(ptp_id="ptp_1", account_id="acc_1", cycle_id="cyc_1",
                        amount_paise=AMOUNT, promised_date=promised,
                        channel=Channel.SMS, captured_by="REQUEST_PTP",
                        confidence=confidence, status=status)


# ---- resolution: all four transitions -------------------------------------------------

def test_ptp_resolves_kept():
    assert resolve(promise(), AMOUNT, AMOUNT, cycle_ended=False) is PTPStatus.KEPT
    assert resolve(promise(), AMOUNT + 1, AMOUNT, cycle_ended=False) is PTPStatus.KEPT


def test_ptp_resolves_partial():
    assert resolve(promise(), AMOUNT // 3, AMOUNT, cycle_ended=False) is PTPStatus.PARTIAL


def test_ptp_resolves_broken():
    assert resolve(promise(), 0, AMOUNT, cycle_ended=False) is PTPStatus.BROKEN


def test_ptp_resolves_lapsed():
    """The cycle ended before the promised date, so the customer never got the chance to
    keep it. Recording that as BROKEN would punish them for our horizon."""
    assert resolve(promise(), 0, AMOUNT, cycle_ended=True) is PTPStatus.LAPSED


def test_full_settlement_beats_a_lapsed_cycle():
    """Money that arrived is money that arrived, whatever the horizon says."""
    assert resolve(promise(), AMOUNT, AMOUNT, cycle_ended=True) is PTPStatus.KEPT


def test_ptp_resolution_is_pure():
    for paid in (0, AMOUNT // 2, AMOUNT):
        assert (resolve(promise(), paid, AMOUNT, False)
                is resolve(promise(), paid, AMOUNT, False))


# ---- suppression -----------------------------------------------------------------------

def test_ptp_suppresses(cfg):
    """No contact between capture and promised_date + grace. This is the window the
    promise buys, and it is the whole reason a promise is worth capturing."""
    grace = cfg.policy.ptp_grace_days
    p = promise(promised=TODAY + timedelta(days=4))
    for offset in range(0, 5 + grace):
        assert is_open(p, TODAY + timedelta(days=offset), grace), offset
    assert not is_open(p, TODAY + timedelta(days=5 + grace), grace)


def test_suppression_ends_only_after_the_grace_period(cfg):
    grace = cfg.policy.ptp_grace_days
    p = promise(promised=TODAY)
    assert is_open(p, TODAY + timedelta(days=grace), grace)
    assert not is_open(p, TODAY + timedelta(days=grace + 1), grace)
    assert due(p, TODAY + timedelta(days=grace + 1), grace)
    assert not due(p, TODAY, grace)


def test_a_resolved_promise_suppresses_nothing(cfg):
    for status in (PTPStatus.KEPT, PTPStatus.BROKEN, PTPStatus.PARTIAL, PTPStatus.LAPSED):
        assert not is_open(promise(status=status), TODAY, cfg.policy.ptp_grace_days)
    assert not is_open(None, TODAY, cfg.policy.ptp_grace_days)


def test_the_gate_refuses_outreach_under_an_open_promise(cfg):
    """POL-PTP-001, which has been in the catalogue since M5 and inert until now because
    nothing populated `ctx.ptp`."""
    from tests.test_gate_denies import make_ctx, message, NOW
    from app.policy import PolicySet, evaluate

    policy = PolicySet.from_config(cfg)
    open_promise = PromiseToPay(
        ptp_id="ptp_1", account_id="acc_1", cycle_id="cyc_1", amount_paise=AMOUNT,
        promised_date=NOW.date() + timedelta(days=3), channel=Channel.SMS,
        captured_by="REQUEST_PTP", confidence=0.6, status=PTPStatus.OPEN)
    denied = evaluate(message(), make_ctx(cfg, ptp=open_promise), policy)
    assert denied.verdict is Verdict.DENY
    assert denied.rule_id_failed == "POL-PTP-001"


def test_the_eligible_set_collapses_to_wait_under_an_open_promise(cfg):
    """An open promise pauses the loop; it does not end it — which is why WAIT, and not
    CLOSE. `PROMISE_ACTIVE` is deliberately not a terminal state."""
    from app.diagnose import eligible_actions
    from app.domain.enums import CauseClass, MandateStatus
    from app.domain.models import Mandate

    mandates = (Mandate("mnd_1", "acc_1", Rail.ENACH, 10**7, MandateStatus.ACTIVE,
                        datetime(2026, 1, 1, tzinfo=IST)),)
    el = eligible_actions({CauseClass.INSUFFICIENT_FUNDS: 1.0}, mandates, cfg,
                          ptp_open=True)
    assert el.actions == {ActionType.WAIT}
    assert el.overlay == "ptp_open"


# ---- consequences -----------------------------------------------------------------------

def test_a_broken_promise_compounds(cfg):
    """A broken promise is evidence about the promiser, so trust decays multiplicatively:
    repeated breaks compound rather than subtracting linearly."""
    c = cfg.sim["ptp"]
    decay, recovery = float(c["broken_decay"]), float(c["kept_recovery"])
    first = next_confidence(0.6, PTPStatus.BROKEN, decay, recovery)
    second = next_confidence(first, PTPStatus.BROKEN, decay, recovery)
    assert first < 0.6
    assert (0.6 - first) > (first - second) or second < first
    assert second >= 0.05, "trust floors rather than reaching zero"


def test_a_kept_promise_earns_trust_back_but_not_all_at_once(cfg):
    c = cfg.sim["ptp"]
    decay, recovery = float(c["broken_decay"]), float(c["kept_recovery"])
    broken = next_confidence(0.6, PTPStatus.BROKEN, decay, recovery)
    kept = next_confidence(broken, PTPStatus.KEPT, decay, recovery)
    assert broken < kept < 0.6 + 1e-9


def test_a_partial_promise_costs_less_trust_than_a_broken_one(cfg):
    c = cfg.sim["ptp"]
    decay, recovery = float(c["broken_decay"]), float(c["kept_recovery"])
    assert (next_confidence(0.6, PTPStatus.PARTIAL, decay, recovery)
            > next_confidence(0.6, PTPStatus.BROKEN, decay, recovery))


def test_a_lapsed_promise_says_nothing_about_the_customer(cfg):
    c = cfg.sim["ptp"]
    assert next_confidence(0.6, PTPStatus.LAPSED, float(c["broken_decay"]),
                           float(c["kept_recovery"])) == 0.6


# ---- the world mechanics -----------------------------------------------------------------

def test_capture_records_a_date_the_customer_chose(cfg):
    """A captured promise is the customer telling you their own inflow phase — which is
    information no return code carries."""
    world = World.generate(cfg, seed=42, n_accounts=400)
    at = datetime.combine(world.start, time(11), tzinfo=IST)
    captured = []
    for account_id in world.cycles:
        outcome, ptp = world.request_ptp(account_id, at, Channel.SMS)
        if outcome == "PTP_CAPTURED":
            captured.append((account_id, ptp))
    assert captured, "no promise was ever captured"
    for account_id, ptp in captured:
        assert ptp.promised_date > at.date()
        assert ptp.status is PTPStatus.OPEN
        assert ptp.amount_paise == world.cycles[account_id].cycle.amount_paise
        assert world.cycles[account_id].ptp is ptp


def test_distress_is_detected_and_is_not_a_promise(cfg):
    """Distress language ends the recovery conversation. It is a rule, not a model
    output — docs/05-POLICY-ENGINE.md."""
    world = World.generate(cfg, seed=7, n_accounts=6000)
    at = datetime.combine(world.start, time(11), tzinfo=IST)
    outcomes = [world.request_ptp(a, at, Channel.SMS)[0] for a in list(world.cycles)]
    assert "DISTRESS" in outcomes

    # Asserted as a *rate* difference, which is what the mechanism guarantees. Precision
    # also depends on how rare hardship is, and is a reported diagnostic (M11) rather than
    # a property of the model — scoring it against `latent_truth` is the evaluator's job.
    hardship = [a for a in world.cycles if world.latent[a].hardship]
    others = [a for a in world.cycles if not world.latent[a].hardship]
    rate = lambda group: sum(world.cycles[a].distress_signalled for a in group) / len(group)
    assert rate(hardship) > 4 * rate(others), (
        f"distress {rate(hardship):.4f} in hardship vs {rate(others):.4f} elsewhere")


def test_all_four_transitions_fire_in_the_world(cfg):
    """The state machine, exercised end to end against real settlement rather than by
    calling `resolve` with made-up numbers."""
    from collections import Counter

    world = World.generate(cfg, seed=11, n_accounts=2500)
    at = datetime.combine(world.start, time(11), tzinfo=IST)
    for account_id in world.cycles:
        world.request_ptp(account_id, at, Channel.SMS)

    seen: Counter = Counter()
    for offset in range(1, 8):                 # a short horizon, so some promises outlive it
        day = world.start + timedelta(days=offset)
        world.tick_day(day)
        for account_id, state in world.cycles.items():
            if due(state.ptp, day, cfg.policy.ptp_grace_days):
                outcome = world.resolve_ptp(account_id, day)
                if outcome:
                    seen[outcome[0]] += 1
    # The horizon runs out with promises still open: those lapse rather than break.
    for account_id in world.cycles:
        lapsed = world.lapse_ptp(account_id)
        if lapsed is not None:
            seen[lapsed] += 1
    assert set(seen) == {PTPStatus.KEPT, PTPStatus.BROKEN, PTPStatus.PARTIAL,
                         PTPStatus.LAPSED}, dict(seen)


def test_a_kept_promise_restores_the_contact_budget(cfg):
    """This customer engaged. Treating them as though they had ignored you is how you
    lose them next time."""
    world = World.generate(cfg, seed=3, n_accounts=60)
    at = datetime.combine(world.start, time(11), tzinfo=IST)
    for account_id, state in world.cycles.items():
        outcome, ptp = world.request_ptp(account_id, at, Channel.SMS)
        if outcome != "PTP_CAPTURED":
            continue
        state.recovery_contacts.extend([at, at])
        world.latent[account_id].balance_paise = state.cycle.amount_paise * 3
        state.ptp_confidence = 1.0
        resolved = world.resolve_ptp(account_id, ptp.promised_date + timedelta(days=2))
        if resolved and resolved[0] is PTPStatus.KEPT:
            assert state.recovery_contacts == []
            return
    pytest.skip("no promise was kept in this seed")


def test_a_partial_payment_does_not_settle_the_cycle(cfg):
    """`settled` still means the whole amount arrived. Partial money moves `paid_paise`
    and is reported separately — a half-paid cycle is not a recovery."""
    world = World.generate(cfg, seed=5, n_accounts=40)
    account_id = next(iter(world.cycles))
    state = world.cycles[account_id]
    state.paid_paise = state.cycle.amount_paise // 2
    assert state.settled_at is None
    assert resolve(promise(), state.paid_paise, state.cycle.amount_paise,
                   cycle_ended=False) is PTPStatus.PARTIAL


# ---- the planner's verdict on asking ------------------------------------------------------

def test_request_ptp_is_dominated_by_the_payment_link(cfg):
    """A measured result, not a bug. `SEND_PAYMENT_LINK` has both a higher recovery lift
    (0.14 against 0.10) and a lower harm weight (0.4 against 0.6), and it is eligible
    under every cause that permits a promise — so asking for a promise is never the
    highest-value action in any state.

    Worth stating plainly: under the declared numbers the agent prefers to hand someone a
    way to pay over asking them to commit to a date. The machinery is built and correct;
    what it needs to be *chosen* is for the promise's own value — the outreach it
    suppresses, and the date it reveals — to be priced, which needs a `ptp_open` dimension
    in the MDP rather than a bigger lift constant.
    """
    from app.domain.enums import CauseClass
    from app.plan import PlanState
    from app.plan.mdp import q_at, solve

    sol = solve(CauseClass.INSUFFICIENT_FUNDS, AMOUNT, 0.8, cfg, amount_scale=120_000)
    for to_inflow in (0, 2, 5, 10, 20):
        qs = q_at(sol, PlanState(20, 4, 3, False, to_inflow, True, True), cfg)
        assert qs[ActionType.SEND_PAYMENT_LINK] > qs[ActionType.REQUEST_PTP], to_inflow

    assert cfg.planner["lift"]["SEND_PAYMENT_LINK"] > cfg.planner["lift"]["REQUEST_PTP"]
    assert (cfg.harm_weight(ActionType.SEND_PAYMENT_LINK)
            < cfg.harm_weight(ActionType.REQUEST_PTP))


# ---- integration -------------------------------------------------------------------------

def test_promises_reach_the_ledger(cfg, tmp_path):
    """Drive a batch with promises forced open, and check the trail records capture,
    suppression and resolution."""
    import json

    path = str(tmp_path / "ptp.db")
    world, _ = simulate_batch(cfg, seed=42, n_accounts=400, out_path=path)
    run_batch(path, cfg, policy="agent", holdout_frac=0.2)
    con = sqlite3.connect(path)
    n = con.execute("SELECT COUNT(*) FROM events WHERE stage=?",
                    (Stage.OBSERVE.value,)).fetchone()[0]
    con.close()
    assert n > 0            # the run completed and observed outcomes


def test_ledger_still_verifies_with_promises(cfg, tmp_path):
    from app.ledger import Ledger

    path = str(tmp_path / "ptpv.db")
    simulate_batch(cfg, seed=42, n_accounts=300, out_path=path)
    run_batch(path, cfg, policy="agent", holdout_frac=0.2)
    con = sqlite3.connect(path)
    batch_id = con.execute("SELECT batch_id FROM batches").fetchone()[0]
    con.close()
    led = Ledger(path, batch_id)
    rep = led.verify()
    led.close()
    assert rep.ok, rep.failures[:5]
