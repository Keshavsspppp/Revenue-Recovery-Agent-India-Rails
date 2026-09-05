"""M2 acceptance, and the stop-the-line test.

    On a fresh batch with the do-nothing policy (notices + default retries only),
    holdout recovery within a 30-day horizon must land in 0.30-0.50 of at-risk value.

If it is near zero, the simulator is lying and every downstream number is inflated. If it
is near one, the holdout is unbeatable and there is no agent worth building. Treat a
failure here as a stop-the-line event: fix the simulator before touching anything else.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.domain.config import Config
from app.domain.enums import CauseClass, Channel
from app.eval.metrics import load_rows
from app.runner import run_batch
from app.sim.generate import simulate_batch
from app.sim.world import World, at_risk_paise, recovered_paise

SELF_CURE_BAND = (0.30, 0.50)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


def run_default(cfg: Config, tmp_path, seed: int, n: int = 2000):
    """Drive the do-nothing policy through the *real* pipeline — runner, compliance gate,
    ledger — and read the outcome back from the events. Measuring self-cure through a
    simulator-only shortcut would leave the stop-the-line number describing code the
    batch does not actually run."""
    path = str(tmp_path / f"selfcure_{seed}_{n}.db")
    simulate_batch(cfg, seed=seed, n_accounts=n, out_path=path)
    run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
    return load_rows(path)


@pytest.fixture(scope="module")
def default_run(cfg: Config, tmp_path_factory):
    return run_default(cfg, tmp_path_factory.mktemp("selfcure"), seed=42, n=2000)


def test_selfcure_lands_in_band(default_run):
    """STOP THE LINE if this fails. Do not tune anything downstream until it passes."""
    rate = (sum(r.recovered_paise for r in default_run)
            / sum(r.amount_paise for r in default_run))
    lo, hi = SELF_CURE_BAND
    assert lo < rate < hi, (
        f"do-nothing self-cure is {rate:.3f}, outside {SELF_CURE_BAND}. "
        "Near zero means the simulator is lying and every downstream number is "
        "inflated. Near one means there is nothing for the agent to win.")


def test_selfcure_is_stable_across_seeds(cfg: Config, tmp_path):
    """A band that only holds on the seed you tuned against is not a band."""
    rates = []
    for seed in (1, 99):
        rows = run_default(cfg, tmp_path, seed=seed, n=800)
        rates.append(sum(r.recovered_paise for r in rows)
                     / sum(r.amount_paise for r in rows))
    lo, hi = SELF_CURE_BAND
    assert all(lo < r < hi for r in rates), rates


def test_both_self_cure_mechanisms_contribute(default_run):
    """Self-cure must come from the merchant's own retries *and* customer self-pay.
    If either is ~0 the holdout is a straw man in one direction or the other."""
    sources = Counter(r.settled_source for r in default_run)
    assert sources["rail"] > 100 and sources["self_pay"] > 100, sources


def test_first_failure_mix_matches_the_declared_target(cfg: Config):
    """The defect mix is a generation parameter and a stated calibration target, so it is
    checked against what actually went wrong — not against what the bank reported."""
    world = World.generate(cfg, seed=42, n_accounts=4000)
    seen = Counter(world.latent[a].true_cause for a in world.cycles)
    for name, target in cfg.sim["defect_mix"].items():
        actual = seen[CauseClass(name)] / 4000
        assert abs(actual - target) < 0.03, f"{name}: {actual:.3f} vs target {target}"


def test_the_observed_mix_is_noisier_than_the_true_one(cfg: Config):
    """What you can see is not what happened. The reported mix is the declared mix blurred
    by code noise, and any diagnosis built on codes inherits that blur — which is the
    entire reason the cause taxonomy is a layer rather than a lookup."""
    from app.domain.codemap import load_codemap

    world = World.generate(cfg, seed=42, n_accounts=4000)
    cm = load_codemap()
    truth = Counter(world.latent[a].true_cause for a in world.cycles)
    reported = Counter(cm.cause_of(s.first_failure_code) for s in world.cycles.values())
    assert truth != reported
    drift = max(abs(truth[c] - reported[c]) / 4000 for c in CauseClass)
    assert drift > 0.01, "code noise is not reaching the reported mix"


# ---- the rule that makes the whole thing honest --------------------------------

def test_a_message_can_never_move_balance(cfg: Config):
    """docs/03-SIMULATOR.md's one rule. If a contact could move balance, the agent could
    message money into existence and every result would be worthless."""
    world = World.generate(cfg, seed=3, n_accounts=50)
    account_id = next(iter(world.accounts))
    latent = world.latent[account_id]
    before = latent.balance_paise
    at = world.cycles[account_id].cycle.due_date

    from datetime import datetime, time
    from app.domain.clock import IST
    when = datetime.combine(at, time(11), tzinfo=IST)
    for channel in (Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL):
        world.contact(account_id, channel, when)
    assert latent.balance_paise == before


def test_contact_moves_intent_and_annoyance(cfg: Config):
    """The other half of the asymmetry: contact does do *something*, just not that."""
    from datetime import datetime, time
    from app.domain.clock import IST

    world = World.generate(cfg, seed=3, n_accounts=50)
    account_id = next(iter(world.accounts))
    latent = world.latent[account_id]
    intent_before, annoy_before = latent.intent, latent.annoyance
    world.contact(account_id, Channel.WHATSAPP,
                  datetime.combine(world.start, time(11), tzinfo=IST))
    assert latent.annoyance > annoy_before
    assert latent.intent > intent_before


def test_a_bare_notice_informs_without_asking(cfg: Config):
    """A pre-debit notice is mandatory and lifts self-pay, but it does not ask the
    customer for anything — so it annoys without lifting intent."""
    from datetime import datetime, time
    from app.domain.clock import IST

    world = World.generate(cfg, seed=3, n_accounts=50)
    account_id = next(iter(world.accounts))
    latent = world.latent[account_id]
    intent_before = latent.intent
    world.contact(account_id, Channel.SMS,
                  datetime.combine(world.start, time(9), tzinfo=IST), lifts_intent=False)
    assert latent.intent == intent_before
    assert latent.annoyance > 0


def test_the_mandatory_notice_lifts_self_pay(cfg: Config):
    """An awkward, real finding worth reporting: some of what a naive system credits to
    clever messaging is caused by the notification the regulator forced it to send."""
    from app.sim.latent import selfpay_hazard

    world = World.generate(cfg, seed=3, n_accounts=10)
    account_id = next(iter(world.accounts))
    latent = world.latent[account_id]
    amount = world.cycles[account_id].cycle.amount_paise
    quiet = selfpay_hazard(latent, amount, False, cfg.sim)
    notified = selfpay_hazard(latent, amount, True, cfg.sim)
    assert notified > quiet


def test_annoyance_suppresses_self_pay(cfg: Config):
    """Hammering people makes them disengage. This is what bends the lambda frontier."""
    from app.sim.latent import selfpay_hazard

    world = World.generate(cfg, seed=3, n_accounts=10)
    account_id = next(iter(world.accounts))
    latent = world.latent[account_id]
    amount = world.cycles[account_id].cycle.amount_paise
    calm = selfpay_hazard(latent, amount, False, cfg.sim)
    latent.annoyance = 0.9
    assert selfpay_hazard(latent, amount, False, cfg.sim) < calm


def test_opt_out_hazard_is_superlinear_in_annoyance(cfg: Config):
    """Three contacts is cheap, eight is expensive. A linear hazard would not punish
    hammering and the frontier would be a straight line."""
    from app.sim.latent import contact_hazards

    world = World.generate(cfg, seed=3, n_accounts=10)
    latent = world.latent[next(iter(world.accounts))]
    latent.annoyance = 0.2
    low = contact_hazards(latent, cfg.sim)["opt_out"]
    latent.annoyance = 0.4
    mid = contact_hazards(latent, cfg.sim)["opt_out"]
    latent.annoyance = 0.8
    high = contact_hazards(latent, cfg.sim)["opt_out"]
    assert (high - mid) > (mid - low)


def test_hardship_accounts_receive_less_income(cfg: Config, tmp_path):
    """The hardship flag must actually bite, or the hardship detector has nothing to
    find at M11."""
    import sqlite3
    path = str(tmp_path / "hardship.db")
    simulate_batch(cfg, seed=11, n_accounts=1500, out_path=path)
    run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
    con = sqlite3.connect(path)
    hardship = {a for (a,) in con.execute(
        "SELECT account_id FROM latent_truth WHERE hardship=1")}
    con.close()
    rows = load_rows(path)
    assert len(hardship) > 30
    rate_h = (sum(r.settled for r in rows if r.account_id in hardship)
              / max(1, sum(1 for r in rows if r.account_id in hardship)))
    rate_o = (sum(r.settled for r in rows if r.account_id not in hardship)
              / max(1, sum(1 for r in rows if r.account_id not in hardship)))
    assert rate_h < rate_o


def test_determinism(cfg: Config, tmp_path):
    """CLAUDE.md rule 5. Same seed, same config, same outcome — through the full
    pipeline including the gate."""
    def fingerprint(name: str) -> list[tuple]:
        path = str(tmp_path / name)
        simulate_batch(cfg, seed=5, n_accounts=200, out_path=path)
        run_batch(path, cfg, policy="nothing", holdout_frac=0.2)
        return sorted((r.account_id, r.settled, r.settled_source, r.terminal_state,
                       r.cost_paise, r.days_to_recover) for r in load_rows(path))
    assert fingerprint("d1.db") == fingerprint("d2.db")


def test_different_seeds_give_different_worlds(cfg: Config):
    a = World.generate(cfg, seed=1, n_accounts=100)
    b = World.generate(cfg, seed=2, n_accounts=100)
    assert ([s.cycle.amount_paise for s in a.cycles.values()]
            != [s.cycle.amount_paise for s in b.cycles.values()])
