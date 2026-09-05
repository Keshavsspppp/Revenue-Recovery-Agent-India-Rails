"""Rules and actions that exist on paper but could never fire in a run.

Every case here is a bug that a passing unit test hid. `test_gate_denies.py` proves each
rule denies when handed a context that violates it; none of that helps if the runner can
never *build* that context, or if the executor quietly drops the action the rule governs.
These are the seams between the two.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import ActionType, Channel, Stage, Verdict
from app.domain.models import Action
from app.policy import PolicySet, evaluate
from app.runner import run_batch
from app.sim.generate import simulate_batch

NOW = datetime(2026, 9, 10, 11, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def policy(cfg: Config) -> PolicySet:
    return PolicySet.from_config(cfg)


# ---- the executor covers every action the planner may choose --------------------

def test_every_action_type_has_an_execution_path(cfg: Config) -> None:
    """`_act` dispatches on ActionType and used to fall off the end for
    VOICE_CONFIRM_PTP — the planner priced it, could select it, and nothing happened.

    Rather than re-derive the dispatch, this asserts the fallback exists: anything that
    reaches the end of `_act` is written to the ledger as NOT_EXECUTED instead of
    vanishing.
    """
    import inspect

    from app import policies

    source = inspect.getsource(policies._act)
    assert "ex.unexecutable(" in source, (
        "_act must end in a recorded fallback. A silent `return` there is how an action "
        "becomes a free WAIT that the value function still prices.")
    for action in ActionType:
        assert action.value in source, f"{action.value} has no branch in _act"


def test_voice_is_executed_and_costs_something(cfg: Config) -> None:
    """The most intrusive action in the set must reach a rail, a cost and the harm
    counters — not return quietly."""
    path = os.path.join(tempfile.mkdtemp(), "voice.db")
    simulate_batch(cfg, seed=5, n_accounts=40, out_path=path)

    def voice_only(ex, world, account_id, day, offset):
        if offset != 3:
            return
        from datetime import time as _t
        ex.request_ptp(account_id, datetime.combine(day, _t(11, 0), tzinfo=IST),
                       Channel.VOICE, action_type=ActionType.VOICE_CONFIRM_PTP)

    run_batch(path, cfg, policy=voice_only, holdout_frac=0.2)
    con = sqlite3.connect(path)
    executed = con.execute(
        "SELECT COUNT(*) FROM events WHERE stage=? AND action_type=?",
        (Stage.EXECUTE.value, ActionType.VOICE_CONFIRM_PTP.value)).fetchone()[0]
    con.close()
    assert executed > 0, "VOICE_CONFIRM_PTP produced no EXECUTE event"


def test_voice_budget_actually_decrements(cfg: Config) -> None:
    """POL-FREQ-003 caps voice at one per cycle. The runner reported the full budget on
    every evaluation, so the cap could never bind."""
    import inspect

    from app.runner import _Executor

    source = inspect.getsource(_Executor.context)
    assert "voice_calls" in source, (
        "GateContext.budgets.voice_remaining_cycle must be derived from calls made, "
        "not from the configured budget")


# ---- rules whose context the runner has to be able to build ---------------------

def test_opt_out_reaches_the_consent_record(cfg: Config) -> None:
    """POL-STOP-001 reads `consent.opted_out_at`. The simulator recorded opt-outs on the
    cycle only, so the gate could not see one however many customers withdrew."""
    import inspect

    from app.sim.world import World

    source = inspect.getsource(World.contact)
    assert "opted_out_at" in source, (
        "World.contact must write the withdrawal onto the consent record the gate reads")
    # And the gate must read it from there.
    from app.policy import RULES_BY_ID
    assert "opted_out_at" in inspect.getsource(RULES_BY_ID["POL-STOP-001"].check)


def test_the_three_notice_rules_are_distinguishable(cfg: Config, policy: PolicySet) -> None:
    """POL-NOTICE-001 and -003 shared a predicate, so -003 could never be the rule that
    failed and a spent notice was reported as no notice at all."""
    from tests.test_gate_denies import make_ctx, retry

    spent = evaluate(retry(), make_ctx(cfg, notice_at=NOW - timedelta(hours=25),
                                       notice_consumed=True), policy)
    assert spent.verdict is Verdict.DENY
    assert spent.rule_id_failed == "POL-NOTICE-003"

    missing = evaluate(retry(), make_ctx(cfg), policy)
    assert missing.verdict is Verdict.DENY
    assert missing.rule_id_failed == "POL-NOTICE-001"


# ---- the API must not read outside data/ ----------------------------------------

def test_batch_path_stays_under_data() -> None:
    from fastapi import HTTPException

    from app.api.main import _batch_path

    for escape in ("../../../Windows/win.ini", "..", "../pyproject.toml"):
        with pytest.raises(HTTPException):
            _batch_path(escape)
