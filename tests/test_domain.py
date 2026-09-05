"""M0 acceptance: the shared contract holds and the config prices every action."""

from __future__ import annotations

import pytest

from app.cli import build_parser, main
from app.domain.clock import IST, Clock
from app.domain.config import Config
from app.domain.enums import (
    CONTACT_ACTIONS,
    ActionType,
    Channel,
    MerchantCategory,
    Rail,
)
from app.domain.models import Action, AttemptResult, canonical_json, make_id
from app.domain.money import Money, format_inr


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


def test_enums_serialise_as_strings():
    assert ActionType.WAIT == "WAIT"
    assert canonical_json({"a": ActionType.WAIT.value}) == '{"a":"WAIT"}'


def test_every_action_is_priced(cfg: Config):
    """README invariant 5: an action with neither a cost nor a harm weight cannot be
    scheduled, because the planner cannot price it."""
    for a in ActionType:
        channel = Channel.SMS if a is ActionType.SEND_MESSAGE else None
        assert cfg.action_cost_paise(a, channel) >= 0
        assert cfg.harm_weight(a, channel) >= 0.0


def test_config_rejects_an_unpriced_action(cfg: Config):
    broken = Config.load()
    object.__setattr__(broken, "action_costs", {k: v for k, v in cfg.action_costs.items()
                                                if k != ActionType.WAIT.value})
    with pytest.raises(ValueError, match="WAIT"):
        broken.validate()


def test_high_cap_applies_only_to_approved_categories(cfg: Config):
    assert cfg.applicable_cap_paise(Rail.ENACH, MerchantCategory.SUBSCRIPTION) == 1_500_000
    assert cfg.applicable_cap_paise(Rail.ENACH, MerchantCategory.INSURANCE) == 10_000_000


def test_notice_exempt_action_is_not_a_contact():
    """POL-QH-001 exempts the pre-debit notice: it is a regulatory notification, not a
    recovery contact. Encoded here so a future edit that reclassifies it fails."""
    assert ActionType.SEND_PREDEBIT_NOTICE not in CONTACT_ACTIONS
    assert ActionType.VOICE_CONFIRM_PTP in CONTACT_ACTIONS


def test_action_hash_is_stable_and_field_sensitive():
    a = Action(type=ActionType.RETRY_DEBIT, rail=Rail.UPI_AUTOPAY, amount_paise=149900)
    b = Action(type=ActionType.RETRY_DEBIT, rail=Rail.UPI_AUTOPAY, amount_paise=149900)
    c = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=149900)
    assert a.hash() == b.hash() != c.hash()


def test_settlement_truth_not_api_acknowledgement():
    with pytest.raises(ValueError):
        AttemptResult(ok=True, rail_code="SUCCESS", settled_at=None, fee_paise=200)


def test_money_is_paise_and_formats_at_the_edge():
    assert Money.rupees(1499.00) == 149900
    assert format_inr(57820000) == "₹5,78,200.00"
    assert format_inr(-5000) == "-₹50.00"


def test_clock_is_ist_and_monotonic():
    c = Clock.at("2026-09-03T13:12:04+05:30")
    assert c.now().tzinfo is IST
    c.advance(days=1)
    with pytest.raises(ValueError):
        c.advance_to(Clock.at("2026-09-01T00:00:00+05:30").now())


def test_ids_are_deterministic_and_sortable():
    assert make_id("evt", 7) < make_id("evt", 48211)
    assert make_id("acc", 7) == make_id("acc", 7)


def test_cli_help_and_config(capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--help"])
    assert e.value.code == 0
    assert main(["config"]) == 0
    assert "policy_version" in capsys.readouterr().out
