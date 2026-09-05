"""M10 acceptance: the LLM's blast radius, enforced by the shape of the interface.

    The LLM never emits an amount, a phone number, a schedule, or customer-facing text.

There is no field for any of those in the schema. These tests fail if a future edit adds
one — which is the point: a guardrail that lives in a prompt holds ninety-nine times.
"""

from __future__ import annotations

import json

import pytest

from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, MandateStatus, Rail
from app.domain.models import InflowEstimate, Mandate
from app.plan import PlanState
from app.propose import FORBIDDEN_KEYS, GroqProposer, RulesProposer, response_format
from app.propose.schema import SYSTEM_PROMPT


def schema_keys(node, found: set[str] | None = None) -> set[str]:
    """Every property name anywhere in the schema, however deeply nested."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                found.update(value)
            schema_keys(value, found)
    elif isinstance(node, list):
        for item in node:
            schema_keys(item, found)
    return found


def test_no_llm_amounts():
    """The acceptance test. No amount, no schedule, no template, no recipient, no body."""
    present = schema_keys(response_format())
    leaked = present & FORBIDDEN_KEYS
    assert not leaked, f"the proposer schema exposes {leaked} to the model"


def test_the_schema_is_a_closed_set():
    """Strict mode plus `additionalProperties: false` means the model cannot invent a
    field — so it cannot invent an amount even by accident."""
    schema = response_format()["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == set(schema["schema"]["properties"])


def test_the_action_enum_is_the_closed_action_space():
    props = response_format()["json_schema"]["schema"]["properties"]
    assert set(props["action_type"]["enum"]) == {a.value for a in ActionType}


def test_the_rationale_is_bounded():
    """The bound is enforced on the way in, not declared in the schema.

    Strict mode accepts only a subset of JSON Schema and `maxLength`/`maxItems` are not in
    it — including them fails the entire request with an HTTP 400, verified against the
    live API. Checking it in `parse()` is better anyway: a limit the caller enforces holds
    whether or not the provider honours it.
    """
    from app.propose.schema import MAX_EVIDENCE_REFS, MAX_RATIONALE_CHARS, parse

    props = response_format()["json_schema"]["schema"]["properties"]
    for unsupported in ("maxLength", "maxItems", "minimum", "maximum"):
        assert unsupported not in json.dumps(props), unsupported

    out = parse({"action_type": "WAIT", "rationale": "x" * 5000,
                 "evidence_refs": [f"r{i}" for i in range(50)], "confidence": 7.5},
                source="test")
    assert len(out.rationale) == MAX_RATIONALE_CHARS
    assert len(out.evidence_refs) == MAX_EVIDENCE_REFS
    assert out.confidence == 1.0


def test_the_prompt_states_the_asymmetry():
    """The one rule the whole project rests on has to be in the instructions too, or the
    model will happily propose messaging someone who has no money."""
    assert "cannot create money" in SYSTEM_PROMPT
    assert "only choose an action from ELIGIBLE" in SYSTEM_PROMPT
    assert "Do not invent facts, amounts or dates" in SYSTEM_PROMPT


# ---- the payload the model actually sees --------------------------------------------

@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


def payload(cfg: Config) -> dict:
    from datetime import datetime
    from app.domain.clock import IST

    mandates = (Mandate("mnd_1", "acc_1", Rail.ENACH, 500000, MandateStatus.ACTIVE,
                        datetime(2026, 1, 1, tzinfo=IST)),)
    return GroqProposer.user_payload(
        PlanState(20, 4, 3, False, 3, True, True),
        {CauseClass.INSUFFICIENT_FUNDS: 0.8, CauseClass.TRANSIENT_INFRA: 0.2},
        frozenset({ActionType.WAIT, ActionType.SEND_PREDEBIT_NOTICE}),
        amount_paise=149900,
        inflow=InflowEstimate(day_of_month=5, concentration=0.8, n_observations=6),
        mandates=mandates, merchant_category="SUBSCRIPTION")


def test_the_payload_carries_a_band_not_an_amount(cfg):
    """The model does not need the rupee figure to pick an action, and withholding it
    makes emitting one structurally impossible."""
    p = payload(cfg)
    assert p["amount_band"] == "1000-2500"
    text = json.dumps(p)
    assert "149900" not in text and "1499" not in text


def test_the_payload_carries_no_pii(cfg):
    """DPDP data minimisation, made concrete. The model sees a state vector, not a
    person — and that is a good thing to be able to say out loud."""
    p = payload(cfg)
    for key in ("name", "phone", "email", "account_number", "city", "account_id"):
        assert key not in p, key
    assert "acc_1" not in json.dumps(p), "the account id itself leaked into the payload"


def test_the_payload_names_the_eligible_set(cfg):
    assert set(payload(cfg)["eligible"]) == {"WAIT", "SEND_PREDEBIT_NOTICE"}


# ---- client behaviour, without a key or a network -------------------------------------

def reply(action: str = "WAIT", **extra) -> dict:
    body = {"action_type": action, "target_rail": None, "channel": None,
            "rationale": "inflow is three days out", "evidence_refs": ["cause:AP01"],
            "confidence": 0.7, **extra}
    return {"choices": [{"message": {"content": json.dumps(body)}}]}


def proposer(transport, **kw) -> GroqProposer:
    return GroqProposer(api_key="test-key", transport=transport, **kw)


def call(p: GroqProposer, cfg: Config, eligible=None):
    from datetime import datetime
    from app.domain.clock import IST

    mandates = (Mandate("mnd_1", "acc_1", Rail.ENACH, 500000, MandateStatus.ACTIVE,
                        datetime(2026, 1, 1, tzinfo=IST)),)
    return p.propose(
        PlanState(20, 4, 3, False, 3, True, True),
        {CauseClass.INSUFFICIENT_FUNDS: 1.0},
        eligible or frozenset({ActionType.WAIT, ActionType.SEND_PREDEBIT_NOTICE}),
        amount_paise=149900,
        inflow=InflowEstimate(day_of_month=5, concentration=0.8, n_observations=6),
        mandates=mandates, merchant_category="SUBSCRIPTION")


def test_a_valid_proposal_is_accepted(cfg):
    p = proposer(lambda *a: reply("WAIT"))
    result = call(p, cfg)
    assert result is not None
    assert result.action_type is ActionType.WAIT
    assert result.rationale == "inflow is three days out"
    assert result.source.startswith("groq:")


def test_a_proposal_outside_the_eligible_set_is_discarded(cfg):
    """PROPOSER_INVALID. Discard it and let the planner decide — never re-prompt."""
    p = proposer(lambda *a: reply("VOICE_CONFIRM_PTP"))
    assert call(p, cfg) is None
    assert p.invalid == 1


def test_malformed_json_is_discarded(cfg):
    p = proposer(lambda *a: {"choices": [{"message": {"content": "not json"}}]})
    assert call(p, cfg) is None
    assert p.invalid == 1


def test_an_unexpected_shape_is_discarded(cfg):
    p = proposer(lambda *a: {"unexpected": True})
    assert call(p, cfg) is None
    assert p.invalid == 1


def test_the_llm_is_never_looped(cfg):
    """An agent that re-prompts until it gets an answer it likes has no bound on cost or
    latency. One retry, then fall through."""
    attempts = []

    def failing(*args):
        attempts.append(1)
        raise TimeoutError("upstream")

    p = proposer(failing, max_retries=1)
    assert call(p, cfg) is None
    assert len(attempts) == 2, "expected exactly one retry"
    assert p.failures == 2


def test_a_transient_failure_then_success(cfg):
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("first")
        return reply("WAIT")

    p = proposer(flaky)
    assert call(p, cfg) is not None
    assert len(calls) == 2


def test_identical_states_are_cached(cfg):
    """Identical states recur constantly across thousands of accounts; caching cuts both
    the bill and the demo's latency."""
    calls = []

    def counting(*args):
        calls.append(1)
        return reply("WAIT")

    p = proposer(counting)
    first, second = call(p, cfg), call(p, cfg)
    assert len(calls) == 1
    assert p.cache_hits == 1
    assert first.action_type is second.action_type
    assert second.cached and not first.cached


def test_stats_are_reportable(cfg):
    p = proposer(lambda *a: reply("WAIT"))
    call(p, cfg)
    stats = p.stats()
    for key in ("calls", "cache_hits", "invalid", "failures", "latency_p50_ms",
                "latency_p95_ms"):
        assert key in stats


def test_without_a_key_the_repo_still_runs(monkeypatch):
    """`The repo must run end-to-end with no GROQ_API_KEY.` Not a degraded mode — the
    deterministic proposer is the control arm."""
    from app.propose import ProposerUnavailable

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProposerUnavailable, match="control arm"):
        GroqProposer.from_env()


# ---- the deterministic control arm -----------------------------------------------------

def test_the_rules_proposer_needs_nothing(cfg):
    """No model, no arithmetic, no network — and it always returns something, because
    WAIT and CLOSE are in every eligible set."""
    r = RulesProposer()
    out = r.propose(None, {CauseClass.MANDATE_INVALID: 0.9},
                    frozenset({ActionType.REREGISTER_MANDATE, ActionType.WAIT,
                               ActionType.CLOSE}))
    assert out is not None
    assert out.action_type is ActionType.REREGISTER_MANDATE
    assert out.source == "rules"


def test_the_rules_proposer_only_picks_from_the_eligible_set(cfg):
    r = RulesProposer()
    for cause in CauseClass:
        out = r.propose(None, {cause: 1.0}, frozenset({ActionType.WAIT}))
        assert out is None or out.action_type in {ActionType.WAIT}


def test_both_proposers_share_an_interface(cfg):
    """Or the ablation is not comparing like with like."""
    rules = RulesProposer().propose(
        PlanState(20, 4, 3, False, 3, True, True),
        {CauseClass.INSUFFICIENT_FUNDS: 1.0},
        frozenset({ActionType.WAIT}),
        amount_paise=149900,
        inflow=InflowEstimate(day_of_month=5, concentration=0.8, n_observations=6),
        mandates=(), merchant_category="SUBSCRIPTION")
    assert rules is not None and rules.action_type is ActionType.WAIT


# ---- the ablation ------------------------------------------------------------------------

def test_ablation_runs_without_a_key(cfg, tmp_path, monkeypatch):
    """`rr ablate` must produce a table whatever the environment. Without a key the groq
    arm is reported unavailable rather than silently substituted — a comparison with an
    arm quietly replaced by another arm is worse than no comparison."""
    from app.eval.ablate import render, run

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    rows = run(tmp_path, cfg, seed=42, n_accounts=250, bootstrap_n=300)
    by_name = {r.name: r for r in rows}
    assert set(by_name) == {"rules", "planner-argmax", "groq"}
    assert by_name["rules"].available and by_name["planner-argmax"].available
    assert not by_name["groq"].available
    assert "GROQ_API_KEY" in by_name["groq"].note

    text = render(rows, cfg)
    assert "PROPOSER ABLATION" in text
    assert "unavailable" in text
    assert "the MDP over a heuristic" in text


def test_every_ablation_arm_shares_the_holdout(cfg, tmp_path, monkeypatch):
    """Same seed, same world, same arm assignment — only the proposer differs. If the
    holdouts diverged the comparison would be measuring the batch, not the proposer."""
    from app.eval.ablate import run

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    rows = [r for r in run(tmp_path, cfg, seed=7, n_accounts=250, bootstrap_n=300)
            if r.available]
    assert len({round(r.holdout_rate, 9) for r in rows}) == 1


def test_the_ablation_reports_an_interval_per_arm(cfg, tmp_path, monkeypatch):
    """A point estimate of lift without an interval is not a result."""
    from app.eval.ablate import run

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    for row in run(tmp_path, cfg, seed=42, n_accounts=250, bootstrap_n=500):
        if row.available:
            lo, hi = row.ci95
            assert lo < hi
            assert lo <= row.incremental_paise <= hi
