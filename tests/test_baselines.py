"""M7 acceptance: the three baselines, and the independence of the arms.

The margin over these is a more credible claim than a raw recovery rate. `fixed` must beat
`nothing` and `oracle` must beat both — if that ordering breaks, something upstream is
wrong and no amount of planner work will fix it.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.domain.config import Config
from app.domain.enums import ActionType, Arm, Stage
from app.eval.metrics import arm_metrics, load_rows
from app.ledger import Ledger
from app.policies import IMPLEMENTED_NAMES, build, merchant_default
from app.runner import run_batch
from app.sim.generate import simulate_batch

SEED = 42
N = 1200


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def runs(cfg: Config, tmp_path_factory) -> dict[str, list]:
    """All three policies over the identical world, seed and arm assignment."""
    root = tmp_path_factory.mktemp("baselines")
    out = {}
    for policy in IMPLEMENTED_NAMES:
        path = str(root / f"{policy}.db")
        simulate_batch(cfg, seed=SEED, n_accounts=N, out_path=path)
        run_batch(path, cfg, policy=policy, holdout_frac=0.2)
        out[policy] = (path, load_rows(path))
    return out


def treatment_rate(rows) -> float:
    return arm_metrics(rows, Arm.TREATMENT).rate


# ---- arm independence: the one that matters -------------------------------------------

def test_holdout_is_invariant_to_the_treatment_policy(runs):
    """The holdout receives the merchant default whatever the treatment arm runs, so its
    recovered value must be *identical* across all three — to the paisa.

    This caught a real defect. With one shared RNG stream, a treatment policy that acted
    more often consumed more draws and shifted every subsequent draw for holdout accounts
    too: the holdout rate moved between 31.8% and 41.1% across three policies that treat
    it identically. A control group whose outcome depends on what the treatment arm did
    is not a control group.
    """
    recovered = {p: arm_metrics(rows, Arm.HOLDOUT).recovered_paise
                 for p, (_, rows) in runs.items()}
    assert len(set(recovered.values())) == 1, recovered

    settled = {p: tuple(sorted(r.account_id for r in rows
                               if r.arm is Arm.HOLDOUT and r.settled))
               for p, (_, rows) in runs.items()}
    assert len(set(settled.values())) == 1, "the same holdout accounts must recover"


def test_holdout_arms_are_the_same_accounts(runs):
    arms = {p: tuple(sorted((r.account_id, r.arm.value) for r in rows))
            for p, (_, rows) in runs.items()}
    assert len(set(arms.values())) == 1


def test_holdout_only_ever_receives_the_merchant_default(runs):
    for policy, (path, _) in runs.items():
        con = sqlite3.connect(path)
        actions = {a for (a,) in con.execute(
            "SELECT DISTINCT action_type FROM events WHERE stage=? AND arm=?",
            (Stage.EXECUTE.value, Arm.HOLDOUT.value))}
        con.close()
        assert actions <= {ActionType.SEND_PREDEBIT_NOTICE.value,
                           ActionType.RETRY_DEBIT.value}, (policy, actions)


# ---- the ordering ----------------------------------------------------------------------

def recovered_accounts(rows) -> int:
    return sum(1 for r in rows if r.arm is Arm.TREATMENT and r.settled)


def test_fixed_beats_nothing(runs):
    """Compared on the treatment arm, which is a paired comparison over the same accounts
    in the same world — far tighter than differencing two noisy incrementals.

    Counted by *accounts recovered* rather than by value. The value-weighted rate carries
    the batch's heavy tail — a couple of Rs 45,000 EMIs landing either way swamp a 1pp
    effect at this size — so on a test-sized batch it flips sign on noise. The count is
    the same claim with a fraction of the variance; the value figure is the headline and
    it belongs on a full batch, not in a unit test.

    If this fails, something upstream is wrong: more attempts and a nudge should recover
    more than fewer attempts and none. Investigate before touching the planner.
    """
    assert recovered_accounts(runs["fixed"][1]) > recovered_accounts(runs["nothing"][1])


def test_oracle_beats_both(runs):
    """The ceiling. If the oracle does not clear the incumbent comfortably, then knowing
    the customer's balance is worth nothing and the planner has no headroom to chase."""
    oracle = recovered_accounts(runs["oracle"][1])
    assert oracle > recovered_accounts(runs["fixed"][1])
    assert oracle > recovered_accounts(runs["nothing"][1])


def test_the_oracle_has_real_headroom(runs):
    """`agent / oracle` is the share of achievable value captured. That ratio is only
    interesting if the denominator is meaningfully above the incumbent."""
    gap = treatment_rate(runs["oracle"][1]) - treatment_rate(runs["fixed"][1])
    assert gap > 0.03, f"oracle only {gap:.1%} above fixed; too little room to measure"


def test_fixed_spends_more_than_nothing(runs):
    """More attempts and a message cost more. A policy that recovered more for less would
    mean the cost accounting is not wired up."""
    for metric in ("cost_paise", "contacts", "attempts"):
        more = getattr(arm_metrics(runs["fixed"][1], Arm.TREATMENT), metric)
        fewer = getattr(arm_metrics(runs["nothing"][1], Arm.TREATMENT), metric)
        assert more > fewer, metric


# ---- every policy runs the whole pipeline ------------------------------------------------

def test_all_three_verify(runs):
    for policy, (path, _) in runs.items():
        con = sqlite3.connect(path)
        batch_id = con.execute("SELECT batch_id FROM batches").fetchone()[0]
        con.close()
        led = Ledger(path, batch_id)
        rep = led.verify()
        led.close()
        assert rep.ok, (policy, rep.failures[:3])


def test_every_policy_is_recorded_in_the_batch(runs):
    """Six weeks later you have to be able to say which policy produced a number."""
    for policy, (path, _) in runs.items():
        con = sqlite3.connect(path)
        assert con.execute("SELECT policy FROM batches").fetchone()[0] == policy
        con.close()


def test_no_notice_window_violations_under_any_policy(runs):
    from app.eval.metrics import compliance_from_ledger
    for policy, (path, rows) in runs.items():
        assert compliance_from_ledger(path, rows)["notice_window_violations"] == 0, policy


def test_the_gate_denies_the_incumbent_schedule(runs):
    """A calendar-based dunning schedule does not survive contact with the rules. `fixed`
    is refused far more often than the merchant default, which is the point: the
    incumbent looks fine on paper."""
    def denials(path: str) -> int:
        con = sqlite3.connect(path)
        n = con.execute("SELECT COUNT(*) FROM events WHERE stage=? AND rule_failed"
                        " IS NOT NULL", (Stage.GATE.value,)).fetchone()[0]
        con.close()
        return n
    assert denials(runs["fixed"][0]) > denials(runs["nothing"][0])


def test_unbuilt_policies_still_say_so(cfg):
    with pytest.raises(NotImplementedError, match="not built yet"):
        build("voice_first", cfg)


def test_the_oracle_never_leaves_the_simulator():
    """It reads latent balance by definition. Anywhere but app/sim would either break the
    module boundary or, worse, quietly not break it."""
    import app.sim.oracle as mod
    assert mod.__name__.startswith("app.sim.")
    import inspect
    from pathlib import Path
    policies = Path(inspect.getsourcefile(build)).read_text(encoding="utf-8")
    assert "from app.sim.oracle import" in policies
    assert policies.index("def build") < policies.index("from app.sim.oracle import"), (
        "the oracle is imported inside build(), so importing app.policies does not "
        "pull the simulator into an agent code path")


def test_determinism_per_policy(cfg, tmp_path):
    def fingerprint(policy: str, name: str):
        path = str(tmp_path / name)
        simulate_batch(cfg, seed=9, n_accounts=200, out_path=path)
        run_batch(path, cfg, policy=policy, holdout_frac=0.2)
        return sorted((r.account_id, r.settled, r.cost_paise, r.terminal_state)
                      for r in load_rows(path))
    assert fingerprint("fixed", "f1.db") == fingerprint("fixed", "f2.db")
    assert fingerprint("oracle", "o1.db") == fingerprint("oracle", "o2.db")
