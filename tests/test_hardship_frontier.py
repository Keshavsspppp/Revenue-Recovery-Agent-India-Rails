"""M11 acceptance: the hardship detector, scored, and the lambda frontier.

Two things that only mean something together. The frontier shows harm was *priced* rather
than mentioned; the detector shows the agent can tell who should not be priced at all.
"""

from __future__ import annotations

import pytest

from app.diagnose.hardship import HardshipSignals, explain, score, signals_from
from app.domain.codemap import load_codemap
from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, MandateStatus, PTPStatus, Rail
from app.domain.models import Mandate
from app.eval.diagnostics import cause_accuracy, hardship_detector
from app.eval.frontier import DEFAULT_LAMBDAS, monotone_contacts
from app.eval.frontier import run as run_frontier
from app.runner import run_batch
from app.sim.generate import simulate_batch


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def batch(cfg: Config, tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("m11") / "batch.db")
    simulate_batch(cfg, seed=42, n_accounts=2500, out_path=path)
    run_batch(path, cfg, policy="agent", holdout_frac=0.2)
    return path


# ---- the score ---------------------------------------------------------------------

def test_one_signal_is_not_a_case(cfg):
    """A single observation should not be enough to write someone off."""
    threshold = cfg.policy.hardship_threshold
    for signals in (HardshipSignals(other_mandate_failing=True),
                    HardshipSignals(insufficient_funds_count=3),
                    HardshipSignals(broken_promise=True)):
        assert score(signals, cfg) <= threshold, signals


def test_the_passive_pair_clears_the_threshold(cfg):
    """Repeated shortfall plus a second merchant failing in the same window. The
    canonical picture, and the only route that needs nothing from the customer — which
    matters, because the routes that do need something are currently unreachable."""
    signals = HardshipSignals(insufficient_funds_count=2, other_mandate_failing=True)
    assert score(signals, cfg) > cfg.policy.hardship_threshold


def test_distress_alone_clears_the_threshold(cfg):
    """Someone telling you in words that they cannot pay is the strongest signal there
    is, and acting on it is the safe direction."""
    assert score(HardshipSignals(distress_language=True), cfg) > cfg.policy.hardship_threshold


def test_a_broken_promise_then_a_shortfall_compounds(cfg):
    """Neither signal alone says as much as the pair: they committed to a date, missed
    it, and then came up short again."""
    apart = score(HardshipSignals(broken_promise=True), cfg)
    together = score(HardshipSignals(broken_promise=True, insufficient_funds_count=1), cfg)
    assert together > apart


def test_the_score_is_bounded(cfg):
    everything = HardshipSignals(insufficient_funds_count=5, other_mandate_failing=True,
                                 distress_language=True, broken_promise=True)
    assert 0.0 <= score(everything, cfg) <= 1.0


def test_signals_come_from_codes_not_from_truth(cfg):
    codemap = load_codemap()
    signals = signals_from(codes=("AP01", "AP01", "AP66"), codemap=codemap,
                           other_mandate_failing=True, distress_language=False,
                           ptp_status=PTPStatus.BROKEN)
    assert signals.insufficient_funds_count == 2
    assert signals.broken_promise and signals.broken_promise_then_shortfall


def test_the_detector_reads_no_latent_state():
    """`latent.hardship` is the answer, not an input. Scoring against it is the
    evaluator's job, and it happens in a different module for exactly that reason.

    Matched against access patterns rather than the word, because this module's own
    docstring says "nothing here may read latent.hardship" — which is the opposite of a
    violation. Same patterns as tests/test_boundaries.py, deliberately.
    """
    import inspect
    import app.diagnose.hardship as mod
    from tests.test_boundaries import LATENT_ACCESS, code_only

    code = code_only(inspect.getsource(mod))
    for pattern in LATENT_ACCESS:
        assert pattern not in code, pattern
    assert "app.sim" not in code


def test_the_score_explains_itself(cfg):
    """An account routed out of recovery has to be able to say which observations put it
    there."""
    signals = HardshipSignals(insufficient_funds_count=2, other_mandate_failing=True)
    lines = explain(signals, score(signals, cfg))
    assert any(l.startswith("hardship_score:") for l in lines)
    assert "other_mandate_failing:1" in lines
    assert all(":" in l for l in lines)


# ---- the overlay -------------------------------------------------------------------

def test_hardship_routes_out_by_rule_not_by_value(cfg):
    """Pricing this would be asking whether it is *profitable* to stop chasing someone
    who cannot pay. It is not meant to be."""
    from app.domain.enums import TerminalState
    from app.plan import PlanState
    from app.plan.agent import choose

    choice = choose(PlanState(20, 4, 3, False, 3, True, True),
                    {CauseClass.INSUFFICIENT_FUNDS: 1.0},
                    frozenset({ActionType.OFFER_ACCOMMODATION, ActionType.CLOSE}),
                    149900, 0.8, cfg, 120000, overlay="hardship")
    assert choice.action is ActionType.OFFER_ACCOMMODATION
    assert choice.terminal is TerminalState.HARDSHIP
    assert "rule rather than by expected value" in choice.reason


def test_the_overlay_replaces_the_matrix(cfg):
    from app.diagnose import eligible_actions

    mandates = (Mandate("mnd_1", "acc_1", Rail.ENACH, 10**7, MandateStatus.ACTIVE,
                        __import__("datetime").datetime(2026, 1, 1)),)
    el = eligible_actions({CauseClass.INSUFFICIENT_FUNDS: 1.0}, mandates, cfg,
                          hardship_score=0.9)
    assert el.overlay == "hardship"
    assert ActionType.RETRY_DEBIT not in el


# ---- scored against ground truth ----------------------------------------------------

def test_hardship_detector_is_scored_and_reported(batch: str):
    """Free to compute, directly relevant to conduct, and the number is what it is."""
    result = hardship_detector(batch)
    assert result.true_positive > 0, "the detector never fired on a real hardship account"
    assert 0.0 < result.precision <= 1.0
    assert 0.0 < result.recall <= 1.0
    assert 0.03 < result.base_rate < 0.15
    assert set(result.as_dict()) >= {"precision", "recall", "f1", "base_rate",
                                     "true_positive", "false_positive",
                                     "false_negative", "true_negative"}


def test_the_detector_is_precise_rather_than_complete(batch: str):
    """The measured operating point: it is right about most of the accounts it flags, and
    it finds a minority of the ones it should.

    Recall is low for a reason worth naming rather than tuning away. Two of the four
    signals — distress language and a broken promise — can only be observed after a
    REQUEST_PTP, and M10 measured that action as strictly dominated by SEND_PAYMENT_LINK,
    so the planner never takes it. The detector is running on the passive pair alone.
    """
    result = hardship_detector(batch)
    assert result.precision > result.recall


def test_the_detector_appears_on_the_scoreboard(batch: str):
    from app.eval.report import build, render

    board, meta = build(batch, bootstrap_n=200)
    text = render(board, meta)
    assert "hardship detector" in text
    assert "precision" in text and "recall" in text
    assert "scored against latent_truth" in text


def test_cause_accuracy_is_labelled_a_sanity_check(batch: str):
    """`04-CAUSE-TAXONOMY.md` is explicit that this is not the metric that matters."""
    result = cause_accuracy(batch)
    assert 0.7 < result["accuracy"] < 1.0
    assert "sanity check" in result["note"]


# ---- the frontier -------------------------------------------------------------------

@pytest.fixture(scope="module")
def frontier(cfg: Config, tmp_path_factory):
    return run_frontier(tmp_path_factory.mktemp("frontier"), cfg, seed=42,
                        n_accounts=900, lambdas=(0.0, 0.5, 2.0), bootstrap_n=400)


def test_lambda_monotone(frontier):
    """As the harm price rises, contact volume falls. If it does not, harm is not
    actually priced and the frontier plot would be nonsense."""
    assert monotone_contacts(frontier), [(p.lambda_harm, p.contacts) for p in frontier]


def test_harm_falls_with_the_price(frontier):
    ordered = sorted(frontier, key=lambda p: p.lambda_harm)
    assert ordered[0].opt_outs_per_1k >= ordered[-1].opt_outs_per_1k


def test_every_point_shares_the_holdout(frontier):
    """lambda changes what the agent chooses, never what the customers do — which is why
    it is excluded from the world hash. If the holdouts diverged, the frontier would be
    measuring the batch rather than the dial."""
    assert len({round(p.holdout_rate, 9) for p in frontier}) == 1


def test_every_point_carries_an_interval(frontier):
    for p in frontier:
        lo, hi = p.ci95
        assert lo < hi


def test_the_frontier_renders_with_its_harm_counters(cfg, frontier):
    from app.eval.frontier import render

    text = render(frontier, cfg)
    assert "LAMBDA FRONTIER" in text
    assert "opt-outs/1k" in text
    assert "same screen as the money" in text
    assert f"shipped at λ={cfg.lambda_harm}" in text


def test_a_broken_frontier_says_so(cfg):
    """If contacts ever stop falling, the plot must refuse to look convincing."""
    from app.eval.frontier import FrontierPoint, render

    def point(lam: float, contacts: int) -> FrontierPoint:
        return FrontierPoint(lambda_harm=lam, treatment_rate=0.4, holdout_rate=0.4,
                             incremental_paise=0, net_incremental_paise=0, ci95=(-1, 1),
                             cost_paise=0, contacts=contacts, attempts=0,
                             opt_outs_per_1k=0.0, complaints_per_1k=0.0,
                             mandate_cancellations_per_1k=0.0, stopped_early=0)

    broken = [point(0.0, 100), point(1.0, 500)]
    assert not monotone_contacts(broken)
    assert "do not fall monotonically" in render(broken, cfg)
