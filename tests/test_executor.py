"""M4 acceptance: idempotency and the settlement feed.

Idempotency is not a nicety here. Every executed action either costs money (an attempt
fee) or lands on a customer's phone, and both are irreversible. A retried run loop or a
crash-restart must not do either twice.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta

import pytest

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import ActionType, Arm, Stage
from app.eval.metrics import days_to_recover, load_rows
from app.runner import AlreadyRun, _Executor, run_batch
from app.sim.generate import simulate_batch
from app.sim.world import ATTEMPT_TIME, NOTICE_TIME, World, provisional_gate
from app.ledger import Ledger


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def batch(tmp_path_factory, cfg: Config) -> str:
    path = str(tmp_path_factory.mktemp("m4") / "batch.db")
    simulate_batch(cfg, seed=42, n_accounts=600, out_path=path)
    run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
    return path


def _count(path: str, **where) -> int:
    con = sqlite3.connect(path)
    clause = " AND ".join(f"{k}=?" for k in where)
    n = con.execute(f"SELECT COUNT(*) FROM events WHERE {clause}",
                    tuple(where.values())).fetchone()[0]
    con.close()
    return n


# ---- idempotency ---------------------------------------------------------------

def test_idempotent_execute(tmp_path, cfg: Config):
    """The same action twice on the same day produces one attempt and one EXECUTE."""
    path = str(tmp_path / "idem.db")
    world, batch_id = simulate_batch(cfg, seed=3, n_accounts=1, out_path=path)
    led = Ledger(path, batch_id)
    account_id = next(iter(world.cycles))
    led.append(__import__("app.ledger", fromlist=["EventDraft"]).EventDraft(
        stage=Stage.ASSIGN, occurred_at=datetime.combine(world.start, time(10), tzinfo=IST),
        account_id=account_id, cycle_id=world.cycles[account_id].cycle.cycle_id,
        arm=Arm.TREATMENT))

    executor = _Executor(world, led, {account_id: Arm.TREATMENT}, cfg)
    mandate = world.primary_mandate(account_id)
    at = datetime.combine(world.start, NOTICE_TIME, tzinfo=IST)
    due = datetime.combine(world.start + timedelta(days=1), ATTEMPT_TIME, tzinfo=IST)

    executor.notice(account_id, mandate, at, due)
    executor.notice(account_id, mandate, at, due)          # replay
    executor.notice(account_id, mandate, at, due)          # replay again
    led.close()

    assert executor.duplicates_suppressed == 2
    assert _count(path, stage=Stage.EXECUTE.value, action_type="SEND_PREDEBIT_NOTICE") == 1
    # a suppressed action must not even be gated — no rail call, no event of any kind
    assert _count(path, stage=Stage.GATE.value) == 1
    assert len(world.cycles[account_id].notices) == 1


def test_idempotency_survives_a_restart(tmp_path, cfg: Config):
    """Keys are rebuilt from the ledger, so a crash-restart cannot double-charge.
    In-process de-duplication alone would silently allow it."""
    path = str(tmp_path / "restart.db")
    world, batch_id = simulate_batch(cfg, seed=3, n_accounts=1, out_path=path)
    from app.ledger import EventDraft
    account_id = next(iter(world.cycles))
    at = datetime.combine(world.start, NOTICE_TIME, tzinfo=IST)
    due = datetime.combine(world.start + timedelta(days=1), ATTEMPT_TIME, tzinfo=IST)

    led = Ledger(path, batch_id)
    led.append(EventDraft(stage=Stage.ASSIGN, occurred_at=at, account_id=account_id,
                          cycle_id=world.cycles[account_id].cycle.cycle_id,
                          arm=Arm.TREATMENT))
    _Executor(world, led, {account_id: Arm.TREATMENT}, cfg).notice(
        account_id, world.primary_mandate(account_id), at, due)
    led.close()

    # process dies here; a fresh executor over the same database must not repeat the work
    led2 = Ledger(path, batch_id)
    executor2 = _Executor(world, led2, {account_id: Arm.TREATMENT}, cfg)
    executor2.notice(account_id, world.primary_mandate(account_id), at, due)
    led2.close()

    assert executor2.duplicates_suppressed == 1
    assert _count(path, stage=Stage.EXECUTE.value, action_type="SEND_PREDEBIT_NOTICE") == 1


def test_the_same_action_on_a_later_day_is_not_a_duplicate(tmp_path, cfg: Config):
    """The key is per (account, cycle, action_type, day). Two notices a week apart are
    two legitimate actions, not a replay."""
    path = str(tmp_path / "days.db")
    world, batch_id = simulate_batch(cfg, seed=3, n_accounts=1, out_path=path)
    from app.ledger import EventDraft
    account_id = next(iter(world.cycles))
    led = Ledger(path, batch_id)
    led.append(EventDraft(stage=Stage.ASSIGN,
                          occurred_at=datetime.combine(world.start, time(10), tzinfo=IST),
                          account_id=account_id,
                          cycle_id=world.cycles[account_id].cycle.cycle_id,
                          arm=Arm.TREATMENT))
    executor = _Executor(world, led, {account_id: Arm.TREATMENT}, cfg)
    mandate = world.primary_mandate(account_id)
    for offset in (0, 4):
        day = world.start + timedelta(days=offset)
        executor.notice(account_id, mandate,
                        datetime.combine(day, NOTICE_TIME, tzinfo=IST),
                        datetime.combine(day + timedelta(days=1), ATTEMPT_TIME, tzinfo=IST))
    led.close()
    assert executor.duplicates_suppressed == 0
    assert _count(path, stage=Stage.EXECUTE.value, action_type="SEND_PREDEBIT_NOTICE") == 2


def test_a_completed_batch_refuses_a_second_run(tmp_path, cfg: Config):
    """The world is regenerated from the seed while idempotency keys come from the
    ledger, so a resumed run would suppress actions the fresh world never performed and
    the two would silently disagree. Refuse loudly rather than pretend to resume."""
    path = str(tmp_path / "rerun.db")
    simulate_batch(cfg, seed=7, n_accounts=120, out_path=path)
    run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
    before = _count(path, stage=Stage.EXECUTE.value)
    cost_before = sum(r.cost_paise for r in load_rows(path))

    with pytest.raises(AlreadyRun, match="already been run"):
        run_batch(path, cfg, policy="nothing", holdout_frac=0.2)

    assert _count(path, stage=Stage.EXECUTE.value) == before
    assert sum(r.cost_paise for r in load_rows(path)) == cost_before


def test_arms_are_read_back_never_recomputed(tmp_path, cfg: Config):
    """docs/02-LEDGER.md: the arm is written at ASSIGN and never derived at read time.
    If the runner recomputed it, a config or code change could move accounts between
    arms mid-batch, which is exactly how a holdout gets corrupted."""
    import sqlite3
    from app.eval.arms import assign_arms, stratum_of
    from app.domain.enums import MerchantCategory

    path = str(tmp_path / "arms.db")
    simulate_batch(cfg, seed=7, n_accounts=200, out_path=path)
    run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
    rows = load_rows(path)

    # a different holdout fraction would have produced a different split entirely
    strata = {r.account_id: stratum_of(r.cause, r.amount_paise,
                                       MerchantCategory(r.category)) for r in rows}
    other = assign_arms(strata, 0.5, 7)
    assert {r.account_id: r.arm for r in rows} != other

    con = sqlite3.connect(path)
    n_assign = con.execute("SELECT COUNT(*) FROM events WHERE stage=?",
                           (Stage.ASSIGN.value,)).fetchone()[0]
    con.close()
    assert n_assign == len(rows)


# ---- the settlement feed -------------------------------------------------------

def test_settlement_feed_reports_only_settled_money(cfg: Config):
    world = World.generate(cfg, seed=42, n_accounts=20)
    start = datetime.combine(world.start, time(0), tzinfo=IST)
    for offset, account_id in enumerate(list(world.cycles)[:5]):
        world.settle(account_id, start + timedelta(days=offset * 3, hours=12),
                     "rail" if offset % 2 else "self_pay")
    feed = world.rails.settlement_feed(start)
    assert len(feed) == 5
    assert all(s.settled_at is not None and s.amount_paise > 0 for s in feed)
    assert {s.source for s in feed} == {"rail", "self_pay"}


def test_settlement_feed_respects_since(cfg: Config):
    """`since` is what lets the runner drain the feed one day at a time rather than
    re-reading the whole batch's settlements every day."""
    world = World.generate(cfg, seed=42, n_accounts=20)
    start = datetime.combine(world.start, time(0), tzinfo=IST)
    for offset, account_id in enumerate(list(world.cycles)[:6]):
        world.settle(account_id, start + timedelta(days=offset * 3, hours=12), "rail")
    everything = world.rails.settlement_feed(start)
    cutoff = start + timedelta(days=10)
    later = world.rails.settlement_feed(cutoff)
    assert 0 < len(later) < len(everything)
    assert all(s.settled_at >= cutoff for s in later)


def test_settlement_observations_do_not_double_count(batch: str):
    """The feed writes an OBSERVE per settlement so the timeline shows when money
    arrived. Those rows carry no `ok`, so exactly one row per account contributes to the
    scoreboard's SUM."""
    con = sqlite3.connect(batch)
    total_observes = con.execute("SELECT COUNT(*) FROM events WHERE stage=?",
                                 (Stage.OBSERVE.value,)).fetchone()[0]
    settled_rows = con.execute(
        "SELECT COUNT(*) FROM events WHERE stage=? AND settled IS NOT NULL",
        (Stage.OBSERVE.value,)).fetchone()[0]
    duplicates = con.execute(
        "SELECT COUNT(*) FROM (SELECT account_id FROM events WHERE stage=? AND"
        " settled IS NOT NULL GROUP BY account_id HAVING COUNT(*) > 1)",
        (Stage.OBSERVE.value,)).fetchone()[0]
    con.close()
    assert total_observes > settled_rows      # the feed added rows
    assert duplicates == 0                    # but none of them can be summed


def test_days_to_recover_is_measured_from_settlement(batch: str):
    rows = load_rows(batch)
    d = days_to_recover(rows, Arm.TREATMENT)
    assert d["n"] > 0
    assert 0 <= d["p50"] <= d["p90"] <= 31
    assert all(r.days_to_recover is None for r in rows if not r.settled)
    assert all(r.days_to_recover is not None for r in rows if r.settled)


def test_ledger_still_verifies_with_the_feed(batch: str):
    con = sqlite3.connect(batch)
    batch_id = con.execute("SELECT batch_id FROM batches").fetchone()[0]
    con.close()
    led = Ledger(batch, batch_id)
    rep = led.verify()
    led.close()
    assert rep.ok, rep.failures[:5]
