"""M6 acceptance: the code map, the posterior, and the eligible-action matrix.

The matrix is the gate between diagnosis and proposal. If it lets through an action that
cannot possibly work — a retry on a structurally dead mandate, a message to someone whose
bank timed out — then the planner is optimising over a set that contains lies, and the
LLM is choosing from one too.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.diagnose import (
    ALLOWED,
    HARDSHIP_SET,
    UNIVERSAL,
    WRONG,
    AccountHistory,
    as_evidence,
    code_prior,
    eligible_actions,
    explain,
    observed_features,
    plausible,
    posterior,
    top,
)
from app.domain.clock import IST
from app.domain.codemap import load_codemap
from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, MandateStatus, Rail
from app.domain.models import Mandate

AT = datetime(2026, 9, 4, 11, 0, tzinfo=IST)
PEAK = AT.replace(hour=20)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def codemap():
    return load_codemap()


def mandates(status: MandateStatus = MandateStatus.ACTIVE, rail: Rail = Rail.ENACH,
             extra: tuple[Mandate, ...] = ()) -> tuple[Mandate, ...]:
    return (Mandate("mnd_1", "acc_1", rail, 10_000_000, status,
                    AT - timedelta(days=200)), *extra)


def history(**kw) -> AccountHistory:
    kw.setdefault("amount_paise", 149900)
    kw.setdefault("last_success_at", AT - timedelta(days=25))
    return AccountHistory(**kw)


def post(code: str, cfg, codemap, at=AT, **kw) -> dict:
    return posterior(code, Rail.ENACH, history(attempts=((at, code),), **kw), at,
                     cfg, codemap)


# ---- Layer 1 ---------------------------------------------------------------------

def test_layer1_maps_codes_to_causes(codemap):
    assert codemap.cause_of("AP01") is CauseClass.INSUFFICIENT_FUNDS
    assert codemap.cause_of("AP17") is CauseClass.MANDATE_INVALID
    assert codemap.cause_of("AP53") is CauseClass.MANDATE_REVOKED
    assert codemap.cause_of("AP66") is CauseClass.TRANSIENT_INFRA


def test_unmapped_counted(codemap, cfg):
    """A taxonomy that silently swallows unknowns is how the drift problem hides."""
    assert codemap.is_unmapped("AP99")
    assert codemap.cause_of("AP99") is CauseClass.UNKNOWN
    assert not codemap.is_unmapped("AP01")

    from app.eval.metrics import AccountRow, compliance_counters
    rows = [AccountRow(account_id=f"a{i}", arm=None, amount_paise=1, cause=CauseClass.UNKNOWN,
                       band="0-500", category="UTILITY", city_tier=1, settled=False,
                       settled_source=None, cost_paise=0, contacts=0, attempts=0,
                       opted_out=False, complained=False, disputed=False,
                       mandate_cancelled=False, terminal_state="CYCLE_ENDED",
                       first_failure_code=code, days_to_recover=None)
            for i, code in enumerate(["AP99", "ZZ9", "AP01"])]
    assert compliance_counters(rows)["unmapped_code_count"] == 2


def test_an_unmapped_code_does_not_masquerade_as_a_diagnosis(cfg, codemap):
    """It tells you the taxonomy moved, not what went wrong. The prior must stay flat
    rather than concentrating on UNKNOWN as though that were an answer."""
    dist = post("AP99", cfg, codemap)
    assert top(dist) is CauseClass.UNKNOWN
    assert dist[CauseClass.UNKNOWN] < 0.5, "an unmapped code is not strong evidence"


# ---- Layer 2 posterior --------------------------------------------------------------

def test_posterior_is_a_distribution(cfg, codemap):
    for code in ("AP01", "AP17", "AP66", "AP99", "ZM"):
        dist = post(code, cfg, codemap)
        assert abs(sum(dist.values()) - 1.0) < 1e-9
        assert all(0.0 <= p <= 1.0 for p in dist.values())
        assert set(dist) == set(CauseClass)


def test_posterior_is_pure(cfg, codemap):
    assert post("AP01", cfg, codemap) == post("AP01", cfg, codemap)


def test_code_prior_puts_declared_mass_on_the_mapped_class(cfg, codemap):
    weight = float(cfg.raw["diagnose"]["code_prior_weight"])
    prior = code_prior("AP01", Rail.ENACH, codemap, weight)
    assert prior[CauseClass.INSUFFICIENT_FUNDS] == pytest.approx(weight)


def test_repeated_same_code_sharpens_insufficient_funds(cfg, codemap):
    """Repeated AP01 shifts weight toward a persistent timing problem and away from
    'transient' — genuinely transient things stop repeating."""
    once = posterior("AP01", Rail.ENACH, history(attempts=((AT, "AP01"),)), AT, cfg,
                     codemap)
    twice = posterior("AP01", Rail.ENACH,
                      history(attempts=((AT, "AP01"), (AT, "AP01"))), AT, cfg, codemap)
    assert twice[CauseClass.INSUFFICIENT_FUNDS] > once[CauseClass.INSUFFICIENT_FUNDS]
    assert twice[CauseClass.TRANSIENT_INFRA] < once[CauseClass.TRANSIENT_INFRA]


def test_peak_hour_raises_transient_infra(cfg, codemap):
    midday = post("AP66", cfg, codemap, at=AT)
    evening = post("AP66", cfg, codemap, at=PEAK)
    assert evening[CauseClass.TRANSIENT_INFRA] > midday[CauseClass.TRANSIENT_INFRA]


def test_tier3_raises_transient_infra(cfg, codemap):
    metro = post("AP66", cfg, codemap, city_tier=1)
    tier3 = post("AP66", cfg, codemap, city_tier=3)
    assert tier3[CauseClass.TRANSIENT_INFRA] > metro[CauseClass.TRANSIENT_INFRA]


def test_amount_above_prior_max_raises_limit_exceeded(cfg, codemap):
    normal = post("AP01", cfg, codemap, max_prior_success_paise=500000)
    large = post("AP01", cfg, codemap, amount_paise=900000,
                 max_prior_success_paise=500000)
    assert large[CauseClass.LIMIT_EXCEEDED] > normal[CauseClass.LIMIT_EXCEEDED]


def test_a_second_failing_mandate_raises_terminal_and_funds(cfg, codemap):
    alone = post("AP01", cfg, codemap)
    together = post("AP01", cfg, codemap, other_mandate_failing=True)
    assert together[CauseClass.ACCOUNT_TERMINAL] > alone[CauseClass.ACCOUNT_TERMINAL]
    assert together[CauseClass.INSUFFICIENT_FUNDS] > alone[CauseClass.INSUFFICIENT_FUNDS]


def test_a_freshly_registered_mandate_raises_auth_artefact(cfg, codemap):
    old = post("AP39", cfg, codemap, mandate_registered_at=AT - timedelta(days=300))
    fresh = post("AP39", cfg, codemap, mandate_registered_at=AT - timedelta(days=3))
    assert fresh[CauseClass.AUTH_ARTEFACT] > old[CauseClass.AUTH_ARTEFACT]


def test_a_changing_code_raises_instability(cfg, codemap):
    steady = posterior("AP01", Rail.ENACH,
                       history(attempts=((AT, "AP01"), (AT, "AP01"))), AT, cfg, codemap)
    changing = posterior("AP01", Rail.ENACH,
                         history(attempts=((AT, "AP66"), (AT, "AP01"))), AT, cfg, codemap)
    assert changing[CauseClass.UNKNOWN] > steady[CauseClass.UNKNOWN]


def test_features_are_named_and_reconstructable(cfg):
    h = history(attempts=((AT, "AP01"), (AT, "AP01")), city_tier=3,
                other_mandate_failing=True)
    features = observed_features(h, PEAK, cfg)
    assert "repeated_same_code" in features
    assert "tier3" in features
    assert "other_mandate_failing" in features
    assert "peak_hour" in features
    evidence = as_evidence("AP01", h, PEAK, cfg)
    assert "rail_code:AP01" in evidence
    assert all(":" in e for e in evidence), "evidence must be a code, a count or a bucket"


def test_posterior_uses_no_latent_state():
    """Structural: the module must not name anything from the simulator's truth."""
    import inspect
    import app.diagnose.posterior as mod
    src = inspect.getsource(mod)
    for forbidden in ("inflow_day", "burn_rate", "latent", "app.sim", "hardship=",
                      "dispute_prone"):
        assert forbidden not in src.replace("hardship_score", ""), forbidden


# ---- the eligible-action matrix -------------------------------------------------------

def test_eligible_excludes_wrong_mandate_invalid(cfg, codemap):
    """No number of attempts fixes an NRE account. This is the acceptance case."""
    dist = post("AP17", cfg, codemap)
    el = eligible_actions(dist, mandates(MandateStatus.INVALID), cfg)
    assert ActionType.RETRY_DEBIT not in el
    assert ActionType.SPLIT_DEBIT not in el
    assert Rail.ENACH in el.forbidden_rails
    assert ActionType.REREGISTER_MANDATE in el     # repair it onto another rail instead


def test_eligible_excludes_wrong_transient_infra(cfg, codemap):
    """Nothing is wrong on the customer's side, so do not message them."""
    dist = post("AP66", cfg, codemap)
    el = eligible_actions(dist, mandates(), cfg)
    assert ActionType.SEND_MESSAGE not in el
    assert ActionType.REQUEST_PTP not in el
    assert ActionType.VOICE_CONFIRM_PTP not in el
    assert ActionType.RETRY_DEBIT in el


def test_insufficient_funds_never_yields_a_message(cfg, codemap):
    """Messages cannot create money. The whole project rests on this asymmetry."""
    dist = post("AP01", cfg, codemap)
    el = eligible_actions(dist, mandates(), cfg)
    assert ActionType.SEND_MESSAGE not in el
    assert ActionType.WAIT in el
    assert ActionType.SEND_PAYMENT_LINK in el      # a route to pay, not a nag


def test_limit_exceeded_never_retries_the_same_amount(cfg, codemap):
    """When the registered cap really is exceeded, the code is corroborated and retrying
    the same amount over the same cap is futile."""
    over_cap = AccountHistory(attempts=((AT, "AP58"),), amount_paise=149900,
                              mandate_status="ACTIVE", mandate_cap_paise=50000,
                              last_success_at=AT - timedelta(days=25))
    dist = posterior("AP58", Rail.ENACH, over_cap, AT, cfg, codemap)
    assert dist[CauseClass.LIMIT_EXCEEDED] > 0.6
    el = eligible_actions(dist, mandates(), cfg)
    assert ActionType.RETRY_DEBIT not in el
    assert ActionType.SPLIT_DEBIT in el


def test_an_over_limit_code_within_the_cap_is_not_believed(cfg, codemap):
    """The mirror case, and the one that matters at 12% code noise: the rail says the
    amount is over the limit, our own registered cap says it is not. The cap is what we
    registered — the code is the weaker witness, and the retry stays on the table."""
    within = AccountHistory(attempts=((AT, "AP58"),), amount_paise=149900,
                            mandate_status="ACTIVE", mandate_cap_paise=600000,
                            afa_free_cap_paise=1500000,
                            last_success_at=AT - timedelta(days=25))
    dist = posterior("AP58", Rail.ENACH, within, AT, cfg, codemap)
    assert dist[CauseClass.LIMIT_EXCEEDED] < 0.35
    assert ActionType.RETRY_DEBIT in eligible_actions(dist, mandates(), cfg)


def test_revoked_never_reregisters_without_consent(cfg, codemap):
    """docs/04 lists this as explicitly wrong; POL-AFA-002 refuses it at the gate too.
    Two independent guards on the same mistake, deliberately."""
    dist = post("AP53", cfg, codemap)
    el = eligible_actions(dist, mandates(MandateStatus.REVOKED), cfg)
    assert ActionType.REREGISTER_MANDATE not in el
    assert ActionType.SEND_MESSAGE in el           # re-consent first


def test_account_terminal_stops_touching_the_rail(cfg, codemap):
    dist = post("AP02", cfg, codemap)
    el = eligible_actions(dist, mandates(MandateStatus.INVALID), cfg)
    assert not ({ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT,
                 ActionType.REREGISTER_MANDATE} & el.actions)
    assert ActionType.ESCALATE_HUMAN in el or ActionType.SEND_PAYMENT_LINK in el


def test_unknown_does_not_escalate(cfg, codemap):
    """You do not know enough yet to spend a human on it."""
    dist = {c: 0.0 for c in CauseClass}
    dist[CauseClass.UNKNOWN] = 1.0
    el = eligible_actions(dist, mandates(), cfg)
    assert ActionType.ESCALATE_HUMAN not in el
    assert ActionType.RETRY_DEBIT in el


def test_a_harm_prohibition_under_any_plausible_cause_wins(cfg, codemap):
    """Doing the wrong thing to a *person* is vetoed on any plausible cause, because the
    cost of being wrong lands on them rather than on a budget."""
    dist = {c: 0.0 for c in CauseClass}
    dist[CauseClass.AUTH_ARTEFACT] = 0.75         # would allow SEND_MESSAGE
    dist[CauseClass.TRANSIENT_INFRA] = 0.25       # forbids it: nothing is wrong their end
    el = eligible_actions(dist, mandates(), cfg)
    assert CauseClass.TRANSIENT_INFRA in el.plausible_causes
    assert ActionType.SEND_MESSAGE not in el


def test_a_futility_prohibition_needs_near_certainty(cfg, codemap):
    """A minority belief that a debit *will not work* must not veto it.

    A wasted retry costs Rs 2.50 and the planner prices that exactly — it solves per
    cause and counts only the mass where the action can succeed. Vetoing on a 25% claim
    forfeits a 75% chance at the whole cycle, which is miscalibration rather than caution,
    and with 12% code noise the codes make that claim wrongly all the time.
    """
    dist = {c: 0.0 for c in CauseClass}
    dist[CauseClass.INSUFFICIENT_FUNDS] = 0.75
    dist[CauseClass.MANDATE_INVALID] = 0.25       # says a retry is futile
    el = eligible_actions(dist, mandates(), cfg)
    assert CauseClass.MANDATE_INVALID in el.plausible_causes
    assert ActionType.RETRY_DEBIT in el, "a 25% futility claim is not a veto"

    convinced = {c: 0.0 for c in CauseClass}
    convinced[CauseClass.MANDATE_INVALID] = 0.9
    convinced[CauseClass.INSUFFICIENT_FUNDS] = 0.1
    assert ActionType.RETRY_DEBIT not in eligible_actions(convinced, mandates(), cfg)


def test_the_records_can_overrule_a_noisy_code(cfg, codemap):
    """The merchant's own registry is a second observation, and the rail did not write it.
    A decline code claiming the mandate is dead while we hold a live one is a code
    disagreeing with a fact — and at 12% code noise that happens constantly."""
    from datetime import timedelta

    def dist_for(status: str):
        h = AccountHistory(attempts=((AT, "AP53"),), amount_paise=149900,
                           mandate_status=status, mandate_cap_paise=500000,
                           afa_free_cap_paise=1500000,
                           mandate_registered_at=AT - timedelta(days=200))
        return posterior("AP53", Rail.ENACH, h, AT, cfg, codemap)

    agrees = dist_for("REVOKED")
    contradicts = dist_for("ACTIVE")
    assert agrees[CauseClass.MANDATE_REVOKED] > 0.6
    assert contradicts[CauseClass.MANDATE_REVOKED] < 0.35
    assert contradicts[CauseClass.INSUFFICIENT_FUNDS] > agrees[CauseClass.INSUFFICIENT_FUNDS]


def test_a_contradiction_is_recorded_as_evidence(cfg):
    """It has to be re-derivable from the ledger, or the audit trail cannot explain why
    the agent overruled the bank."""
    from datetime import timedelta
    h = AccountHistory(attempts=((AT, "AP53"),), amount_paise=149900,
                       mandate_status="ACTIVE",
                       mandate_registered_at=AT - timedelta(days=200))
    ev = as_evidence("AP53", h, AT, cfg)
    assert "contradiction:records_contradict_mandate_death" in ev
    assert "mandate_status:ACTIVE" in ev


def test_an_implausible_cause_does_not_veto(cfg, codemap):
    """Below the threshold it is noise, not a veto — otherwise every eligible set
    collapses to WAIT and the agent never acts."""
    dist = {c: 0.0 for c in CauseClass}
    dist[CauseClass.INSUFFICIENT_FUNDS] = 0.95
    dist[CauseClass.MANDATE_INVALID] = 0.05
    el = eligible_actions(dist, mandates(), cfg)
    assert ActionType.RETRY_DEBIT in el


def test_wait_and_close_are_always_available(cfg, codemap):
    """README invariant 6: an agent that cannot choose to do nothing is firing, not
    optimising. And the stopping rule needs CLOSE to be reachable from every state."""
    for code in ("AP01", "AP17", "AP53", "AP02", "AP66", "AP58", "AP39"):
        el = eligible_actions(post(code, cfg, codemap), mandates(), cfg)
        assert ActionType.WAIT in el and ActionType.CLOSE in el, code


def test_reregister_is_withheld_when_there_is_nowhere_to_move(cfg, codemap):
    """Offering an action that cannot happen would let the planner price a phantom."""
    everywhere = tuple(
        Mandate(f"mnd_{i}", "acc_1", rail, 10_000_000, MandateStatus.INVALID,
                AT - timedelta(days=100))
        for i, rail in enumerate(r for r in Rail if r is not Rail.PAYMENT_LINK))
    el = eligible_actions(post("AP17", cfg, codemap), everywhere, cfg)
    assert ActionType.REREGISTER_MANDATE not in el
    assert not el.target_rails


def test_no_debit_without_a_healthy_mandate(cfg, codemap):
    el = eligible_actions(post("AP01", cfg, codemap), mandates(MandateStatus.REVOKED),
                          cfg)
    assert not ({ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT,
                 ActionType.SEND_PREDEBIT_NOTICE} & el.actions)


# ---- overlays -------------------------------------------------------------------------

def test_hardship_overlay_replaces_the_matrix(cfg, codemap):
    """Knowing who *not* to chase. RBI's draft norms expect lenders to identify borrowers
    in difficulty and offer guidance rather than one more attempt."""
    el = eligible_actions(post("AP01", cfg, codemap), mandates(), cfg,
                          hardship_score=0.8)
    assert el.actions == HARDSHIP_SET
    assert el.overlay == "hardship"
    assert ActionType.RETRY_DEBIT not in el


def test_hardship_overlay_respects_the_threshold(cfg, codemap):
    below = eligible_actions(post("AP01", cfg, codemap), mandates(), cfg,
                             hardship_score=cfg.policy.hardship_threshold)
    assert below.overlay is None


def test_open_ptp_overlay_pauses_everything(cfg, codemap):
    """A promise is a commitment with a verifiable outcome. An open one pauses the loop;
    it does not end it — which is why WAIT, not CLOSE."""
    el = eligible_actions(post("AP01", cfg, codemap), mandates(), cfg, ptp_open=True)
    assert el.actions == {ActionType.WAIT}
    assert el.overlay == "ptp_open"


def test_ptp_overlay_beats_hardship(cfg, codemap):
    el = eligible_actions(post("AP01", cfg, codemap), mandates(), cfg,
                          hardship_score=0.9, ptp_open=True)
    assert el.overlay == "ptp_open"


# ---- structure ------------------------------------------------------------------------

def test_every_cause_has_a_row_in_both_columns():
    for cause in CauseClass:
        assert cause in ALLOWED, cause
        assert cause in WRONG, cause


def test_no_cause_both_allows_and_forbids_an_action():
    for cause in CauseClass:
        overlap = ALLOWED[cause] & WRONG[cause]
        assert not overlap, f"{cause.value} both allows and forbids {overlap}"


def test_the_matrix_never_yields_an_empty_set(cfg, codemap):
    for code in list(load_codemap().mappings)[:20]:
        el = eligible_actions(post(code, cfg, codemap), mandates(), cfg)
        assert len(el) >= len(UNIVERSAL)


def test_explain_produces_ledger_evidence(cfg, codemap):
    el = eligible_actions(post("AP17", cfg, codemap), mandates(MandateStatus.INVALID),
                          cfg)
    lines = explain(el)
    assert any(l.startswith("plausible_cause:") for l in lines)
    assert any(l.startswith("eligible:") for l in lines)
    assert any(l.startswith("forbidden_rail:") for l in lines)
    assert all(":" in l for l in lines)


def test_plausible_never_returns_empty():
    flat = {c: 0.01 for c in CauseClass}
    assert plausible(flat, 0.9)
    assert plausible({}, 0.5) == (CauseClass.UNKNOWN,)


# ---- what Layer 2 actually adds, measured ---------------------------------------------

def test_layer2_is_never_worse_than_layer1(cfg, codemap, tmp_path):
    """Measured, not assumed. Layer 2 currently adds 0.000 over the code map alone, and
    that is the reportable result rather than a failure — the code is right ~88% of the
    time, so a 0.75 prior on it is if anything under-confident, and no observable history
    feature in the set should be strong enough to overturn it.

    What this test protects is the direction: a change to the priors or the likelihood
    ratios must not make diagnosis *worse* than the deterministic map it is built on.
    """
    from datetime import datetime
    from app.sim.generate import simulate_batch

    world, _ = simulate_batch(cfg, seed=7, n_accounts=1200,
                              out_path=str(tmp_path / "l2.db"))
    at = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    l1 = l2 = 0
    for account_id, state in world.cycles.items():
        truth = world.latent[account_id].true_cause
        code = state.first_failure_code
        rail = world.mandates[account_id][0].rail
        hist = AccountHistory(
            attempts=((at, code),), amount_paise=state.cycle.amount_paise,
            city_tier=world.accounts[account_id].city_tier,
            mandate_registered_at=world.mandates[account_id][0].registered_at)
        l1 += codemap.cause_of(code) is truth
        l2 += top(posterior(code, rail, hist, at, cfg, codemap)) is truth
    assert l2 >= l1, (
        f"the posterior is now worse than the code map alone ({l2} vs {l1}). "
        "Layer 1 is the defensible floor; Layer 2 must not degrade it.")


def test_the_simulator_reports_imperfect_codes(cfg):
    """Without code noise a code map is a perfect diagnosis, Layer 2 has nothing to add,
    and the whole taxonomy story is decorative. The doc's premise is that sponsor banks
    populate these inconsistently, so the simulator has to as well."""
    from app.sim.world import World

    world = World.generate(cfg, seed=42, n_accounts=3000)
    cm = load_codemap()
    disagreements = sum(
        1 for a, s in world.cycles.items()
        if cm.cause_of(s.first_failure_code) is not world.latent[a].true_cause)
    rate = disagreements / len(world.cycles)
    assert 0.05 < rate < 0.25, f"reported code disagrees with truth {rate:.1%} of the time"
