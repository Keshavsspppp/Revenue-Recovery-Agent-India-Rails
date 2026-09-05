"""SPLIT_DEBIT: n presentations of one cycle, each under the per-transaction ceiling.

The action exists because a Rs 20,000 debit is refused by a Rs 15,000 AFA-free ceiling
that two of Rs 10,000 clear. It buys nothing against a *balance* — the parts sum to the
cycle amount, so needing the whole amount is needing the whole amount — and the tests
here are written to hold that line: every one of them is about the ceiling, the notice
coupling, or the attempt budget, and none of them claims a funds benefit.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta

import pytest

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import ActionType, MandateStatus, Rail, Stage, Verdict
from app.domain.money import split_parts
from app.sim.generate import simulate_batch

AT = datetime(2026, 9, 10, 10, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


# ---- the arithmetic -------------------------------------------------------------

def test_parts_sum_to_the_amount_exactly() -> None:
    """POL-AMT-001 reconciles the parts against the cycle. A rounding remainder left
    behind is a debit that collects less than it owes."""
    for amount in (149900, 2000000, 3999999, 4500001, 3, 100001):
        for cap in (1500000, 1000000, 2, 999999):
            parts = split_parts(amount, cap, 3)
            if parts:
                assert sum(parts) == amount, (amount, cap, parts)
                assert max(parts) <= cap, (amount, cap, parts)


def test_no_split_when_the_amount_already_fits() -> None:
    assert split_parts(149900, 1500000, 3) == (149900,)


def test_no_split_beyond_the_permitted_number_of_presentations() -> None:
    """An amount that needs four presentations wants a payment link, not a debit."""
    assert split_parts(5000000, 1500000, 3) == ()
    assert len(split_parts(5000000, 1500000, 4)) == 4


# ---- the gate reads presentations, not the cycle total --------------------------

def test_the_afa_ceiling_applies_per_presentation(cfg: Config) -> None:
    from tests.test_gate_denies import make_ctx
    from app.domain.models import Action
    from app.policy import PolicySet, evaluate

    policy = PolicySet.from_config(cfg)
    over = 2_000_000                       # Rs 20,000, over the Rs 15,000 ceiling
    parts = (1_000_000, 1_000_000)

    # One presentation of the whole amount is refused...
    ctx = make_ctx(cfg, notice_at=AT - timedelta(hours=25), amount=over)
    whole = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=over,
                   scheduled_for=AT)
    assert evaluate(whole, ctx, policy).rule_id_failed == "POL-AFA-001"

    # ...and the same money in two is not, given a notice for each part.
    ctx2 = make_ctx(cfg, notice_at=AT - timedelta(hours=25), amount=over,
                    notice_parts=parts)
    split = Action(type=ActionType.SPLIT_DEBIT, rail=Rail.ENACH, amount_paise=over,
                   parts=parts, scheduled_for=AT)
    decision = evaluate(split, ctx2, policy)
    assert decision.verdict is Verdict.ALLOW, decision.rule_id_failed


def test_a_split_needs_a_notice_for_every_part(cfg: Config) -> None:
    """POL-NOTICE-003 is one notice per presentation. Two equal parts need two distinct
    receipts, not one receipt counted twice."""
    from tests.test_gate_denies import make_ctx
    from app.domain.models import Action
    from app.policy import PolicySet, evaluate

    policy = PolicySet.from_config(cfg)
    over, parts = 2_000_000, (1_000_000, 1_000_000)
    split = Action(type=ActionType.SPLIT_DEBIT, rail=Rail.ENACH, amount_paise=over,
                   parts=parts, scheduled_for=AT)

    one_notice = make_ctx(cfg, notice_at=AT - timedelta(hours=25), amount=over,
                          notice_parts=(1_000_000,))
    assert evaluate(split, one_notice, policy).rule_id_failed == "POL-NOTICE-001"


def test_a_split_needs_an_attempt_per_part(cfg: Config) -> None:
    """POL-BUDGET-001 counts presentations, not actions."""
    from tests.test_gate_denies import make_ctx
    from app.domain.models import Action
    from app.policy import PolicySet, evaluate

    policy = PolicySet.from_config(cfg)
    over, parts = 2_000_000, (1_000_000, 1_000_000)
    split = Action(type=ActionType.SPLIT_DEBIT, rail=Rail.ENACH, amount_paise=over,
                   parts=parts, scheduled_for=AT)
    ctx = make_ctx(cfg, notice_at=AT - timedelta(hours=25), amount=over,
                   notice_parts=parts, attempts_remaining=1)
    assert evaluate(split, ctx, policy).rule_id_failed == "POL-BUDGET-001"


# ---- the planner prices what the executor would run -----------------------------

def test_the_planner_prices_a_three_part_split_below_a_two_part_one(cfg: Config) -> None:
    """More presentations is more fees, more harm, more attempts and more chances for
    the rail to refuse. A planner that priced them the same would split too eagerly."""
    from app.domain.enums import CauseClass
    from app.plan.mdp import PlanState, q_at, solution_for

    state = PlanState(days_left=10, attempts_left=3, contacts_left=3, notice_pending=True,
                      days_to_inflow=2, mandate_ok=True, alt_rail=True)
    q = {}
    for n in (2, 3):
        sol = solution_for(CauseClass.LIMIT_EXCEEDED, 2_000_000, 0.6, cfg,
                           amount_scale=2_000_000, split_parts_n=n)
        q[n] = q_at(sol, state, cfg)[ActionType.SPLIT_DEBIT]
    assert q[3] < q[2], q


# ---- end to end -----------------------------------------------------------------

def test_a_split_is_executed_and_collects_the_whole_amount(cfg: Config, tmp_path) -> None:
    """The executor presents each part, the ledger records every return code, and the
    cycle settles only when the whole amount has arrived."""
    from app.runner import run_batch

    path = str(tmp_path / "split.db")
    simulate_batch(cfg, seed=17, n_accounts=60, out_path=path)

    fired: list[str] = []

    def split_once(ex, world, account_id, day, offset):
        state = world.cycles[account_id]
        mandate = world.primary_mandate(account_id)
        if mandate is None or mandate.status is not MandateStatus.ACTIVE:
            return
        # Force an amount that needs two presentations, then notice and split it.
        if offset == 1:
            object.__setattr__(state.cycle, "amount_paise", 2_000_000)
        if offset == 2:
            ex.notice(account_id, mandate, datetime.combine(day, time(9), tzinfo=IST),
                      datetime.combine(day + timedelta(days=1), time(10), tzinfo=IST),
                      split=True)
        if offset == 3:
            ex.split_retry(account_id, mandate,
                           datetime.combine(day, time(10), tzinfo=IST))
            fired.append(account_id)

    run_batch(path, cfg, policy=split_once, holdout_frac=0.2)
    assert fired, "the test policy never reached a healthy mandate"

    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT payload FROM events WHERE stage=? AND action_type=?",
        (Stage.EXECUTE.value, ActionType.SPLIT_DEBIT.value)).fetchall()
    notices = con.execute(
        "SELECT COUNT(*) FROM events WHERE stage=? AND action_type=?",
        (Stage.EXECUTE.value, ActionType.SEND_PREDEBIT_NOTICE.value)).fetchone()[0]
    con.close()

    assert rows, "no SPLIT_DEBIT reached the ledger"
    assert notices, "no notice was issued for the split"

    import json
    max_parts = int(cfg.planner["split_max_parts"])
    seen_ok = seen_partial = False
    for (payload,) in rows:
        result = json.loads(payload)["result"]
        parts = result["parts"]
        assert 2 <= len(parts) <= max_parts, result
        assert sum(parts) == 2_000_000, result
        assert result["presented"] <= len(parts)
        # Collected is a sum of whole parts — never a fraction of one, and never more
        # than was presented.
        assert 0 <= result["collected_paise"] <= sum(parts[:result["presented"]])
        assert result["ok"] == (result["paid_paise"] >= 2_000_000)
        seen_ok = seen_ok or result["ok"]
        seen_partial = seen_partial or 0 < result["collected_paise"] < 2_000_000
    # Both outcomes are real and both must be representable: every part clears, or some
    # do and the cycle stays unsettled with the money that did arrive recorded.
    assert seen_ok or seen_partial, "no split collected anything on any account"


def test_a_partial_split_does_not_settle_the_cycle(cfg: Config, tmp_path) -> None:
    """Part one lands, part two is refused: the cycle is neither settled nor untouched.
    `settled` has to keep meaning the whole amount arrived, or every rate on the
    scoreboard starts counting money that never came."""
    import json

    from app.runner import run_batch

    path = str(tmp_path / "partial.db")
    simulate_batch(cfg, seed=17, n_accounts=60, out_path=path)

    def split_once(ex, world, account_id, day, offset):
        state = world.cycles[account_id]
        mandate = world.primary_mandate(account_id)
        if mandate is None or mandate.status is not MandateStatus.ACTIVE:
            return
        if offset == 1:
            object.__setattr__(state.cycle, "amount_paise", 2_000_000)
        if offset == 2:
            ex.notice(account_id, mandate, datetime.combine(day, time(9), tzinfo=IST),
                      datetime.combine(day + timedelta(days=1), time(10), tzinfo=IST),
                      split=True)
        if offset == 3:
            ex.split_retry(account_id, mandate,
                           datetime.combine(day, time(10), tzinfo=IST))

    run_batch(path, cfg, policy=split_once, holdout_frac=0.2)
    con = sqlite3.connect(path)
    partials = 0
    for (account_id, payload) in con.execute(
            "SELECT account_id, payload FROM events WHERE stage=? AND action_type=?",
            (Stage.EXECUTE.value, ActionType.SPLIT_DEBIT.value)):
        result = json.loads(payload)["result"]
        if 0 < result["collected_paise"] < 2_000_000:
            partials += 1
            assert result["ok"] is False, result
            # Nothing may claim this cycle settled on the rail.
            settled = con.execute(
                "SELECT COUNT(*) FROM events WHERE account_id=? AND settled=1"
                " AND json_extract(payload,'$.result.settled_source')='rail'",
                (account_id,)).fetchone()[0]
            assert settled == 0, f"{account_id} settled on a partial collection"
    con.close()
    assert partials, "no partial collection occurred; the case is untested"


def test_a_split_never_presents_more_than_the_ceiling(cfg: Config, tmp_path) -> None:
    """The point of the action. Every presentation must be under the cap the whole
    amount exceeded — otherwise it is a retry with extra fees."""
    from app.runner import run_batch

    path = str(tmp_path / "ceiling.db")
    simulate_batch(cfg, seed=23, n_accounts=40, out_path=path)
    run_batch(path, cfg, policy="agent", holdout_frac=0.2)

    con = sqlite3.connect(path)
    import json
    for (payload,) in con.execute(
            "SELECT payload FROM events WHERE stage=? AND action_type=?",
            (Stage.EXECUTE.value, ActionType.SPLIT_DEBIT.value)):
        event = json.loads(payload)
        cap = cfg.applicable_cap_paise(Rail.ENACH, __import__(
            "app.domain.enums", fromlist=["MerchantCategory"]
        ).MerchantCategory.LENDING_EMI)
        if cap is not None:
            assert max(event["result"]["parts"]) <= cap, event["result"]
    con.close()
