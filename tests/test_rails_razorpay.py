"""The live rail adapter, tested without credentials.

The seam is the claim this project makes about being more than a simulation: the same
agent, the same gate and the same ledger, with a real provider underneath. These tests
pin the parts of that which must hold whether or not a key is present — the gate is
enforced identically, a live key is refused, and a provider renaming a code never needs a
code change.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from app.domain.clock import IST
from app.domain.codemap import load_codemap
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    CauseClass,
    MandateStatus,
    Rail,
    Verdict,
)
from app.domain.models import Action, GateDecision, Mandate
from app.rails import GateViolation, RailAdapter, require_gate
from app.rails.razorpay import RazorpayTestAdapter, RazorpayUnavailable

AT = datetime(2026, 9, 10, 11, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


def allow(action: Action) -> GateDecision:
    return GateDecision(decision_id="dec_1", action_hash=action.hash(),
                        verdict=Verdict.ALLOW, rule_ids_passed=("POL-QH-001",),
                        rule_id_failed=None, reason=None,
                        policy_version="pol_test", evaluated_at=AT)


def deny(action: Action) -> GateDecision:
    return GateDecision(decision_id="dec_1", action_hash=action.hash(),
                        verdict=Verdict.DENY, rule_ids_passed=(),
                        rule_id_failed="POL-QH-001", reason="quiet hours",
                        policy_version="pol_test", evaluated_at=AT)


def adapter(cfg: Config, responses: dict[str, Any] | None = None) -> RazorpayTestAdapter:
    """A transport that answers like Razorpay, so nothing leaves the machine."""
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, auth: str, payload, timeout):
        calls.append((method, url))
        path = url.rsplit("/v1", 1)[-1]
        if responses and path in responses:
            return responses[path]
        if path == "/customers":
            return {"id": "cust_TEST123"}
        if path == "/payment_links":
            return {"id": "plink_TEST123", "short_url": "https://rzp.io/rzp/TEST"}
        if path == "/subscription_registration/auth_links":
            return {"id": "inv_TEST123", "short_url": "https://rzp.io/i/TEST"}
        if path == "/orders":
            return {"id": "order_TEST123"}
        if path == "/payments/create/recurring":
            return {"id": "pay_TEST123", "status": "captured",
                    "created_at": int(AT.timestamp())}
        return {}

    a = RazorpayTestAdapter(key_id="rzp_test_fake", key_secret="secret", cfg=cfg,
                            transport=transport)
    a._calls_made = calls          # type: ignore[attr-defined]
    return a


# ---- the guarantee that matters most --------------------------------------------------

def test_the_live_adapter_refuses_without_a_gate(cfg):
    """Identical enforcement to the simulator, because it is literally the same function.
    If the live rail could be reached ungated, nothing the simulated run proves would
    transfer."""
    a = adapter(cfg)
    action = Action(type=ActionType.SEND_PAYMENT_LINK)
    with pytest.raises(GateViolation, match="no gate decision"):
        a.payment_link("acc_1", 149900, AT, action, None)


def test_the_live_adapter_refuses_a_denying_gate(cfg):
    a = adapter(cfg)
    action = Action(type=ActionType.SEND_PAYMENT_LINK)
    with pytest.raises(GateViolation):
        a.payment_link("acc_1", 149900, AT, action, deny(action))


def test_the_live_adapter_refuses_a_swapped_action(cfg):
    """A gate signs one action. This is what stops an approved link becoming a debit."""
    a = adapter(cfg)
    signed = Action(type=ActionType.SEND_PAYMENT_LINK)
    swapped = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=999999)
    with pytest.raises(GateViolation, match="action_hash mismatch"):
        a.payment_link("acc_1", 149900, AT, swapped, allow(signed))


def test_both_adapters_share_one_gate_check():
    """Not two implementations that agree today — one function, used by both."""
    import app.sim.rails as sim
    assert sim.SimRailAdapter._require_gate is require_gate
    assert "require_gate" in __import__("inspect").getsource(
        __import__("app.rails.razorpay", fromlist=["x"]))


def test_both_adapters_satisfy_the_protocol(cfg):
    for name in ("notify", "attempt", "mandate_status", "register_mandate",
                 "settlement_feed"):
        assert hasattr(RazorpayTestAdapter, name), name
        assert callable(getattr(RazorpayTestAdapter, name))


# ---- refusing to touch real money -----------------------------------------------------

def test_a_live_key_is_refused(monkeypatch, cfg):
    """A live key would move real money on behalf of real people. The adapter will not
    start on one, whatever the caller intended."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abcdef123456")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    with pytest.raises(RazorpayUnavailable, match="test mode only"):
        RazorpayTestAdapter.from_env(cfg)


def test_missing_credentials_are_not_an_error(monkeypatch, cfg):
    """The simulated rails are the default, and every measured number is produced on
    them. Absence of a key is a configuration state, not a failure."""
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.setattr("app.domain.env.load_dotenv", lambda *a, **k: [])
    with pytest.raises(RazorpayUnavailable, match="simulated rails"):
        RazorpayTestAdapter.from_env(cfg)


# ---- mapping a provider's codes onto our taxonomy -------------------------------------

def test_provider_codes_are_config_not_code():
    """`04-CAUSE-TAXONOMY.md`'s whole premise is that these lists drift. A provider
    renaming a reason must be a config change, never a code change."""
    codemap = load_codemap()
    assert "razorpay" in codemap.providers
    assert codemap.provider_cause("razorpay", "payment_failed") \
        is CauseClass.INSUFFICIENT_FUNDS
    assert codemap.provider_cause("razorpay", "token_expired") is CauseClass.MANDATE_REVOKED
    assert codemap.provider_cause("razorpay", "GATEWAY_ERROR") is CauseClass.TRANSIENT_INFRA


def test_an_unknown_provider_code_is_not_guessed():
    """Falling to UNKNOWN is what makes drift visible on the scoreboard instead of
    silently mis-diagnosed."""
    codemap = load_codemap()
    assert codemap.provider_cause("razorpay", "some_new_reason_2027") is CauseClass.UNKNOWN
    assert codemap.provider_cause("stripe", "payment_failed") is CauseClass.UNKNOWN


def test_a_known_provider_reason_lands_on_a_known_rail_code(cfg):
    """Whatever Razorpay says, a reason we recognise has to land on a code the rest of
    the system can already reason about."""
    a = adapter(cfg)
    codemap = load_codemap()
    for reason in ("payment_failed", "token_expired", "GATEWAY_ERROR"):
        for rail in (Rail.ENACH, Rail.UPI_AUTOPAY, Rail.CARD_EMANDATE):
            code = a._razorpay_code(reason, rail)
            assert not codemap.is_unmapped(code), (reason, rail, code)


def test_an_unrecognised_provider_reason_is_counted_not_invented(cfg):
    """This was a crash. `code_for(rail, UNKNOWN)` has no answer and raises, so the first
    unfamiliar reason Razorpay returned would have taken the run down — on the exact
    scenario the cause taxonomy exists to survive.

    It now comes back verbatim and prefixed, which reads as UNKNOWN, counts as unmapped,
    and puts the drift on the scoreboard rather than in a traceback.
    """
    a = adapter(cfg)
    codemap = load_codemap()
    code = a._razorpay_code("some_new_reason_2027", Rail.ENACH)
    assert code == "RZP:some_new_reason_2027"
    assert codemap.is_unmapped(code)
    assert codemap.cause_of(code) is CauseClass.UNKNOWN
    assert a._razorpay_code("", Rail.UPI_AUTOPAY) == "RZP:unspecified"


# ---- what it actually does ------------------------------------------------------------

def test_a_payment_link_returns_a_real_shaped_reference(cfg):
    a = adapter(cfg)
    action = Action(type=ActionType.SEND_PAYMENT_LINK)
    link = a.payment_link("acc_1", 149900, AT, action, allow(action))
    assert link["id"].startswith("plink_")
    assert link["url"].startswith("https://")
    assert {"kind": "payment_link", "id": "plink_TEST123",
            "url": "https://rzp.io/rzp/TEST", "account_id": "acc_1"} in a.created


def test_a_repaired_mandate_is_pending_until_the_customer_authorises(cfg):
    """The auth link is real and openable, and the mandate is not live until someone
    completes it — which is the AFA step POL-AFA-002 exists to insist on, and the same
    state the simulator returns."""
    a = adapter(cfg)
    action = Action(type=ActionType.REREGISTER_MANDATE, target_rail=Rail.UPI_AUTOPAY)
    mandate = a.register_mandate("acc_1", Rail.UPI_AUTOPAY, 5_000_000, AT, allow(action))
    assert mandate.status is MandateStatus.PENDING_AFA
    assert any(c["kind"] == "auth_link" for c in a.created)


def test_a_debit_without_a_live_notice_never_reaches_the_provider(cfg):
    """The notice window is checked before the call, not after. Presenting a debit we
    already know is non-compliant would be paying Razorpay to tell us so."""
    from app.domain.codemap import NOTICE_WINDOW_VIOLATION

    a = adapter(cfg)
    mandate = Mandate("mnd_1", "acc_1", Rail.ENACH, 10**7, MandateStatus.ACTIVE,
                      AT - timedelta(days=100))
    action = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=149900,
                    scheduled_for=AT)
    before = len(a._calls_made)                      # type: ignore[attr-defined]
    result = a.attempt(mandate, action, AT, allow(action))
    assert result.rail_code == NOTICE_WINDOW_VIOLATION
    assert not result.ok
    assert len(a._calls_made) == before              # type: ignore[attr-defined]


def test_a_notice_then_a_debit_reaches_the_provider(cfg):
    a = adapter(cfg)
    mandate = Mandate("mnd_1", "acc_1", Rail.ENACH, 10**7, MandateStatus.ACTIVE,
                      AT - timedelta(days=100))
    notice_action = Action(type=ActionType.SEND_PREDEBIT_NOTICE, rail=Rail.ENACH,
                           amount_paise=149900, scheduled_for=AT)
    a.notify(mandate, notice_action, AT - timedelta(hours=25), allow(notice_action))
    a._tokens[mandate.mandate_id] = "token_TEST"     # the customer completed the mandate

    debit = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=149900,
                   scheduled_for=AT)
    result = a.attempt(mandate, debit, AT, allow(debit))
    assert result.ok and result.settled_at is not None
    assert any(c["kind"] == "payment" for c in a.created)


def test_settlement_is_read_from_the_feed_not_the_response(cfg):
    a = adapter(cfg)
    mandate = Mandate("mnd_1", "acc_1", Rail.ENACH, 10**7, MandateStatus.ACTIVE,
                      AT - timedelta(days=100))
    notice_action = Action(type=ActionType.SEND_PREDEBIT_NOTICE, rail=Rail.ENACH,
                           amount_paise=149900, scheduled_for=AT)
    a.notify(mandate, notice_action, AT - timedelta(hours=25), allow(notice_action))
    a._tokens[mandate.mandate_id] = "token_TEST"
    debit = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=149900,
                   scheduled_for=AT)
    a.attempt(mandate, debit, AT, allow(debit))

    feed = a.settlement_feed(AT - timedelta(days=1))
    assert len(feed) == 1
    assert feed[0].reference.startswith("pay_")      # the provider's own id, lookupable


def test_every_created_object_is_recorded_for_the_audit_trail(cfg):
    """The point of the live slice: every id it makes can be looked up in the dashboard
    afterwards, so the trail is checkable against a system we do not control."""
    a = adapter(cfg)
    action = Action(type=ActionType.SEND_PAYMENT_LINK)
    a.payment_link("acc_1", 149900, AT, action, allow(action))
    assert all({"kind", "id"} <= set(c) for c in a.created)
    assert a.stats()["created"] == len(a.created)


def test_no_adapter_reads_dotenv_at_construction():
    """A constructor that repopulates the environment from `.env` defeats a test's
    `monkeypatch.delenv` — and that is not a style point.

    It let the suite restore a real key and start making paid API calls against Groq and
    Razorpay, and because the throttle waits ~7s per call the run *hung* rather than
    failing. `app.cli.main` reads the file once at entry; nothing else does.
    """
    import inspect

    import app.propose.groq as groq
    import app.rails.razorpay as rzp

    for module in (groq, rzp):
        source = inspect.getsource(module)
        assert "load_dotenv()" not in source, module.__name__
