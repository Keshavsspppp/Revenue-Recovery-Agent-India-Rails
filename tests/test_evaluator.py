"""M3 acceptance: arms, the three denominators, intervals, and a report that is a *view*.

The central test here is `test_report_matches_ledger`: every reported figure must be
recomputable from the raw events by an independent query. If the report ever becomes a
separate accounting, the audit trail stops being the record.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

import pytest

from app.domain.config import Config
from app.domain.enums import Arm, CauseClass, MerchantCategory, Stage
from app.eval.arms import assign_arms, balance_table, chi_square_imbalance, stratum_of
from app.eval.metrics import (
    UNADDRESSABLE,
    arm_metrics,
    bootstrap_incremental,
    load_rows,
    scoreboard,
)
from app.eval.report import build, render
from app.runner import ConfigDrift, run_batch
from app.sim.generate import simulate_batch


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def batch(tmp_path_factory, cfg: Config) -> str:
    path = str(tmp_path_factory.mktemp("eval") / "batch.db")
    simulate_batch(cfg, seed=42, n_accounts=800, out_path=path)
    run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
    return path


@pytest.fixture(scope="module")
def rows(batch: str):
    return load_rows(batch)


# ---- arms ---------------------------------------------------------------------

def _strata(n: int = 600):
    causes = list(CauseClass)[:6]
    cats = list(MerchantCategory)
    return {f"acc_{i:05d}": stratum_of(causes[i % len(causes)],
                                       (i % 7 + 1) * 30000,
                                       cats[i % len(cats)])
            for i in range(n)}


def test_assignment_is_deterministic_given_the_seed():
    strata = _strata()
    assert assign_arms(strata, 0.2, 42) == assign_arms(strata, 0.2, 42)
    assert assign_arms(strata, 0.2, 42) != assign_arms(strata, 0.2, 43)


def test_holdout_fraction_is_respected():
    arms = assign_arms(_strata(2000), 0.2, 42)
    share = sum(a is Arm.HOLDOUT for a in arms.values()) / len(arms)
    assert 0.17 < share < 0.23, share


def test_small_strata_still_contribute_holdouts():
    """A plain round(n * frac) gives a stratum of three accounts zero holdouts every
    time, which biases the control group toward the common causes."""
    strata = {f"acc_{i}": stratum_of(CauseClass.MANDATE_INVALID, 30000 * (i + 1),
                                     MerchantCategory.UTILITY) for i in range(3)}
    seen = {sum(a is Arm.HOLDOUT for a in assign_arms(strata, 0.2, seed).values())
            for seed in range(40)}
    assert seen != {0}, "small strata never yield a holdout"


def test_stratification_keeps_arms_balanced(rows):
    """Simple randomisation on 2,000 accounts can imbalance the cause classes badly
    enough to swamp the effect being measured."""
    strata = {r.account_id: stratum_of(r.cause, r.amount_paise,
                                       MerchantCategory(r.category)) for r in rows}
    arms = {r.account_id: r.arm for r in rows}
    # 8 cause classes -> 7 df; 14.07 is the 0.95 critical value
    assert chi_square_imbalance(strata, arms) < 14.07


def test_balance_table_is_reportable(rows):
    strata = {r.account_id: stratum_of(r.cause, r.amount_paise,
                                       MerchantCategory(r.category)) for r in rows}
    table = balance_table(strata, {r.account_id: r.arm for r in rows})
    assert table and sum(t + h for *_, t, h in table) == len(rows)


def test_holdout_frac_must_be_a_fraction():
    with pytest.raises(ValueError):
        assign_arms(_strata(10), 0.0, 1)
    with pytest.raises(ValueError):
        assign_arms(_strata(10), 1.0, 1)


# ---- the report is a view over the ledger --------------------------------------

def test_report_matches_ledger(batch: str, rows):
    """Every headline figure recomputed by an independent query against raw events."""
    board = scoreboard(rows, bootstrap_n=200)
    con = sqlite3.connect(batch)

    # the query from docs/02-LEDGER.md, written against the events table alone
    ledger_totals = {
        arm: (accounts, recovered) for arm, accounts, recovered in con.execute(
            "SELECT arm, COUNT(DISTINCT account_id),"
            " SUM(CASE WHEN settled=1 THEN amount_paise ELSE 0 END)"
            " FROM events WHERE stage=? GROUP BY arm", (Stage.OBSERVE.value,))}

    assert ledger_totals["treatment"][0] == board.treatment.accounts
    assert ledger_totals["treatment"][1] == board.treatment.recovered_paise
    assert ledger_totals["holdout"][0] == board.holdout.accounts
    assert ledger_totals["holdout"][1] == board.holdout.recovered_paise

    # `settled IS NOT NULL` picks the one settlement-carrying OBSERVE per cycle. The
    # settlement-feed rows deliberately leave it NULL precisely so sums like this one
    # cannot count an account twice.
    at_risk = dict(con.execute(
        "SELECT e.arm, SUM(c.amount_paise) FROM events e JOIN cycles c"
        " ON c.account_id = e.account_id WHERE e.stage=? AND e.settled IS NOT NULL"
        " GROUP BY e.arm", (Stage.OBSERVE.value,)))
    assert at_risk["treatment"] == board.treatment.at_risk_paise
    assert at_risk["holdout"] == board.holdout.at_risk_paise
    con.close()


def test_costs_are_recomputable_from_execute_events(batch: str, rows):
    con = sqlite3.connect(batch)
    total = 0
    for (payload,) in con.execute("SELECT payload FROM events WHERE stage=?",
                                  (Stage.EXECUTE.value,)):
        total += int((json.loads(payload).get("result") or {}).get("cost_paise") or 0)
    con.close()
    assert total == sum(r.cost_paise for r in rows)


def test_metrics_sum(rows):
    """Per-segment recovered values must sum to the total."""
    board = scoreboard(rows, bootstrap_n=100)
    for name in ("cause", "amount", "category", "tier"):
        seg_at_risk = sum(s["at_risk_paise"] for s in board.segments[name])
        assert seg_at_risk == board.treatment.at_risk_paise + board.holdout.at_risk_paise
        assert sum(s["accounts"] for s in board.segments[name]) == len(rows)


def test_every_account_has_exactly_one_settlement_observation(batch: str):
    """More than one row carrying `ok` would double-count the account in the SUM."""
    con = sqlite3.connect(batch)
    counts = con.execute(
        "SELECT account_id, COUNT(*) FROM events WHERE stage=? AND settled IS NOT NULL"
        " GROUP BY account_id HAVING COUNT(*) > 1", (Stage.OBSERVE.value,)).fetchall()
    con.close()
    assert not counts


def test_every_cycle_closes_with_exactly_one_terminal_state(batch: str, rows):
    con = sqlite3.connect(batch)
    n = con.execute("SELECT COUNT(*) FROM events WHERE stage=?",
                    (Stage.CLOSE.value,)).fetchone()[0]
    con.close()
    assert n == len(rows)
    assert all(r.terminal_state for r in rows)


# ---- the three denominators ----------------------------------------------------

def test_three_denominators_are_ordered(rows):
    """per_retried >= per_addressable >= per_all_failed. The last one is the honest one,
    and a vendor quoting only the first is quoting the flattering one."""
    d = scoreboard(rows, bootstrap_n=100).denominators
    assert d["per_retried"][0] >= d["per_all_failed"][0]
    assert d["per_addressable"][0] >= d["per_all_failed"][0]
    assert d["per_retried"][1] <= d["per_all_failed"][1]


def test_addressable_excludes_terminal_and_revoked(rows):
    addressable = [r for r in rows if r.addressable]
    assert all(r.cause not in UNADDRESSABLE for r in addressable)
    assert len(addressable) < len(rows)


# ---- intervals -----------------------------------------------------------------

def test_bootstrap_returns_an_interval_containing_the_point(rows):
    board = scoreboard(rows, bootstrap_n=2000)
    lo, hi = board.ci95
    assert lo < hi
    assert lo <= board.incremental_recovered_paise <= hi


def test_bootstrap_is_stable_across_seeds(rows):
    """Two seeds must give overlapping intervals, or the resample count is too low."""
    a = bootstrap_incremental(rows, n=2000, seed=1)
    b = bootstrap_incremental(rows, n=2000, seed=2)
    assert a[0] < b[1] and b[0] < a[1], (a, b)


def test_rate_difference_ci_is_symmetric_about_the_point(rows):
    board = scoreboard(rows, bootstrap_n=100)
    lo, hi = board.rate_ci95
    t = [r for r in rows if r.arm is Arm.TREATMENT]
    h = [r for r in rows if r.arm is Arm.HOLDOUT]
    point = sum(r.settled for r in t) / len(t) - sum(r.settled for r in h) / len(h)
    assert abs((lo + hi) / 2 - point) < 1e-9


# ---- the A/A null --------------------------------------------------------------

def test_identical_policies_produce_no_significant_lift(rows):
    """Both arms ran the *same* policy, so this batch is an A/A test. If the interval
    excluded zero here, the evaluator would be manufacturing lift out of nothing and
    every later result would be worthless."""
    board = scoreboard(rows, bootstrap_n=3000)
    lo, hi = board.ci95
    assert lo <= 0 <= hi, (
        f"A/A run shows a significant lift: {board.incremental_recovered_paise} "
        f"CI [{lo}, {hi}]. The evaluator is inventing an effect.")


# ---- harm and compliance -------------------------------------------------------

def test_harm_counters_are_present_for_both_arms(rows):
    board = scoreboard(rows, bootstrap_n=100)
    for arm in ("treatment", "holdout"):
        for key in ("opt_outs_per_1k", "complaints_per_1k", "disputes_per_1k",
                    "mandate_cancellations_per_1k", "contacts_p95"):
            assert key in board.harm[arm]


def test_notice_window_violations_are_zero(batch: str):
    """Our own defect counter. Non-zero means the scheduler has a bug."""
    board, _ = build(batch, bootstrap_n=100)
    assert board.compliance["notice_window_violations"] == 0


def test_holdout_arm_is_never_treated_beyond_the_default(batch: str):
    con = sqlite3.connect(batch)
    actions = {a for (a,) in con.execute(
        "SELECT DISTINCT action_type FROM events WHERE stage=? AND arm=?",
        (Stage.EXECUTE.value, Arm.HOLDOUT.value))}
    con.close()
    assert actions <= {"SEND_PREDEBIT_NOTICE", "RETRY_DEBIT"}, actions


# ---- the runner ----------------------------------------------------------------

def test_run_refuses_on_config_drift(tmp_path, cfg: Config):
    """A batch simulated under one config and run under another compares two different
    worlds, because the world is regenerated from the seed."""
    path = str(tmp_path / "drift.db")
    simulate_batch(cfg, seed=5, n_accounts=40, out_path=path)
    drifted = Config.load()
    object.__setattr__(drifted, "raw", {**cfg.raw, "policy_version": "pol_tampered"})
    with pytest.raises(ConfigDrift, match="config has changed"):
        run_batch(path, drifted, policy="nothing")


def test_unbuilt_policies_say_so(tmp_path, cfg: Config):
    path = str(tmp_path / "p.db")
    simulate_batch(cfg, seed=5, n_accounts=20, out_path=path)
    with pytest.raises(NotImplementedError, match="not built yet"):
        run_batch(path, cfg, policy="voice_first")


def test_run_is_deterministic(tmp_path, cfg: Config):
    def fingerprint(name: str) -> list[tuple]:
        path = str(tmp_path / name)
        simulate_batch(cfg, seed=11, n_accounts=150, out_path=path)
        run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
        return [(r.account_id, r.arm, r.settled, r.cost_paise, r.terminal_state)
                for r in sorted(load_rows(path), key=lambda r: r.account_id)]
    assert fingerprint("a.db") == fingerprint("b.db")


def test_report_renders(batch: str):
    board, meta = build(batch, bootstrap_n=200)
    text = render(board, meta, segment="cause")
    for expected in ("INCREMENTAL RECOVERED", "95% CI", "self-cure baseline",
                     "denominator printed", "HARM", "COMPLIANCE", "TERMINAL STATES"):
        assert expected in text


def test_no_denominator_can_exceed_one(rows):
    """A recovery rate above 100% means the numerator and the denominator describe
    different sets of accounts. `per_retried` did exactly that until its numerator
    stopped counting accounts that self-paid without ever being attempted."""
    for name, (rate, n) in scoreboard(rows, bootstrap_n=100).denominators.items():
        assert 0.0 <= rate <= 1.0, f"{name} = {rate:.1%} on n={n}"
