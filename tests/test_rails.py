"""The rail adapter: gate enforcement and the attempt-resolution order.

The order matters because it decides which code you observe when several things are
wrong at once, and the code you observe is the only diagnosis you get.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from app.domain.clock import IST
from app.domain.codemap import NOTICE_WINDOW_VIOLATION, SUCCESS, load_codemap
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    CauseClass,
    MandateStatus,
    MerchantCategory,
    Rail,
    Verdict,
)
from app.domain.models import Action, GateDecision
from app.sim.rails import GateViolation
from app.sim.world import World, provisional_gate

AT = datetime(2026, 9, 4, 10, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture()
def world(cfg: Config) -> World:
    return World.generate(cfg, seed=42, n_accounts=60)


def _setup(world: World, account_id: str, *, balance: int | None = None,
           notice_at: datetime | None = None, amount: int | None = None):
    """Give an account a clean ACTIVE mandate, a balance, and optionally a live notice."""
    state = world.cycles[account_id]
    if amount is not None:
        state.cycle = type(state.cycle)(**{**state.cycle.__dict__, "amount_paise": amount})
    mandate = world._replace_mandate(world.mandates[account_id][0],
                                     status=MandateStatus.ACTIVE, defect=None,
                                     cap_paise=max(state.cycle.amount_paise * 3, 10_000_000))
    if balance is not None:
        world.latent[account_id].balance_paise = balance
    if notice_at is not None:
        world.send_notice(account_id, mandate, notice_at, AT,
                          lambda a, t: provisional_gate(a, t, "pol_test"))
    return mandate, state.cycle.amount_paise


def _attempt(world: World, mandate, amount: int, at: datetime = AT, gate=None):
    action = Action(type=ActionType.RETRY_DEBIT, rail=mandate.rail, amount_paise=amount,
                    scheduled_for=at)
    return world.rails.attempt(mandate, action, at,
                               gate if gate is not None else provisional_gate(action, at, "pol_test"))


# ---- the gate is the only path to a rail --------------------------------------

def test_adapter_refuses_without_a_gate(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9,
                             notice_at=AT - timedelta(hours=25))
    action = Action(type=ActionType.RETRY_DEBIT, rail=mandate.rail, amount_paise=amount,
                    scheduled_for=AT)
    with pytest.raises(GateViolation, match="no gate decision"):
        world.rails.attempt(mandate, action, AT, None)


def test_adapter_refuses_a_denying_gate(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9,
                             notice_at=AT - timedelta(hours=25))
    action = Action(type=ActionType.RETRY_DEBIT, rail=mandate.rail, amount_paise=amount,
                    scheduled_for=AT)
    denied = GateDecision(decision_id="dec_1", action_hash=action.hash(),
                          verdict=Verdict.DENY, rule_ids_passed=(),
                          rule_id_failed="POL-QH-001", reason="quiet hours",
                          policy_version="pol_test", evaluated_at=AT)
    with pytest.raises(GateViolation):
        world.rails.attempt(mandate, action, AT, denied)


def test_adapter_refuses_a_swapped_action(world: World):
    """A gate signs one action. Executing a different one is not covered by it — this is
    what stops an approved 149900 becoming an executed 4200000."""
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9,
                             notice_at=AT - timedelta(hours=25))
    signed = Action(type=ActionType.RETRY_DEBIT, rail=mandate.rail, amount_paise=amount,
                    scheduled_for=AT)
    gate = provisional_gate(signed, AT, "pol_test")
    swapped = Action(type=ActionType.RETRY_DEBIT, rail=mandate.rail,
                     amount_paise=amount * 10, scheduled_for=AT)
    with pytest.raises(GateViolation, match="action_hash mismatch"):
        world.rails.attempt(mandate, swapped, AT, gate)


# ---- attempt resolution order --------------------------------------------------

def test_no_notice_is_rejected_at_the_rail(world: World):
    """Our own defect, not the customer's. Must be 0 on the scoreboard."""
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9)
    result = _attempt(world, mandate, amount)
    assert result.rail_code == NOTICE_WINDOW_VIOLATION and result.fee_paise == 0


def test_notice_23h_before_is_still_a_violation(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9,
                             notice_at=AT - timedelta(hours=23))
    assert _attempt(world, mandate, amount).rail_code == NOTICE_WINDOW_VIOLATION


def test_notice_24h_before_unlocks_the_attempt(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9,
                             notice_at=AT - timedelta(hours=24))
    assert _attempt(world, mandate, amount).rail_code != NOTICE_WINDOW_VIOLATION


def test_one_notice_is_consumed_by_one_attempt(world: World):
    """POL-NOTICE-003, the load-bearing assumption. A second attempt needs a second
    notice — and a *failed* attempt spends the notice just as a successful one does."""
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=0,
                             notice_at=AT - timedelta(hours=25))
    first = _attempt(world, mandate, amount)
    assert first.rail_code != NOTICE_WINDOW_VIOLATION and not first.ok
    second = _attempt(world, mandate, amount, at=AT + timedelta(hours=1))
    assert second.rail_code == NOTICE_WINDOW_VIOLATION


def test_structural_failure_beats_insufficient_funds(world: World):
    """A revoked mandate with an empty account reports AP53, not AP01: real rails fail
    fast on structure before they touch the account. Getting this backwards would send
    the agent chasing money for a mandate that can never debit."""
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=0,
                             notice_at=AT - timedelta(hours=25))
    mandate = world._replace_mandate(mandate, status=MandateStatus.REVOKED)
    result = _attempt(world, mandate, amount)
    assert load_codemap().cause_of(result.rail_code) is CauseClass.MANDATE_REVOKED


def test_limit_beats_funds(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=0,
                             notice_at=AT - timedelta(hours=25))
    mandate = world._replace_mandate(mandate, cap_paise=amount // 2)
    result = _attempt(world, mandate, amount)
    assert load_codemap().cause_of(result.rail_code) is CauseClass.LIMIT_EXCEEDED


def test_afa_free_cap_applies_by_category(world: World, cfg: Config):
    """Above the AFA-free ceiling the debit cannot run unattended. Insurance and the
    other approved categories get the higher one."""
    account_id = next(iter(world.accounts))
    amount = 2_000_000                                  # Rs 20,000: over the 15,000 cap
    mandate, amount = _setup(world, account_id, balance=10**9, amount=amount,
                             notice_at=AT - timedelta(hours=25))
    account = world.accounts[account_id]
    world.accounts[account_id] = type(account)(
        **{**account.__dict__, "merchant_category": MerchantCategory.SUBSCRIPTION})
    assert load_codemap().cause_of(_attempt(world, mandate, amount).rail_code) \
        is CauseClass.LIMIT_EXCEEDED

    world.accounts[account_id] = type(account)(
        **{**account.__dict__, "merchant_category": MerchantCategory.INSURANCE})
    world.send_notice(account_id, mandate, AT - timedelta(hours=25), AT,
                      lambda a, t: provisional_gate(a, t, "pol_test"))
    assert _attempt(world, mandate, amount).ok


def test_funds_are_the_last_check_and_the_only_latent_read(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=0,
                             notice_at=AT - timedelta(hours=25))
    assert load_codemap().cause_of(_attempt(world, mandate, amount).rail_code) \
        is CauseClass.INSUFFICIENT_FUNDS


def test_success_debits_the_balance_and_settles(world: World):
    account_id = next(iter(world.accounts))
    mandate, amount = _setup(world, account_id, balance=10**9,
                             notice_at=AT - timedelta(hours=25))
    before = world.latent[account_id].balance_paise
    result = _attempt(world, mandate, amount)
    assert result.ok and result.rail_code == SUCCESS
    assert result.settled_at is not None            # settlement, not an API 200
    assert world.latent[account_id].balance_paise == before - amount


def test_infra_failure_peaks_in_the_evening(world: World, cfg: Config):
    """19:00-22:00 load, and tier 3 materially below metro. Both are declared
    assumptions from vendor-published figures, and both live in config."""
    evening = datetime(2026, 9, 4, 20, 0, tzinfo=IST)
    midday = datetime(2026, 9, 4, 13, 0, tzinfo=IST)
    assert world.rails.p_infra(evening, 1) > world.rails.p_infra(midday, 1)
    assert world.rails.p_infra(midday, 3) > world.rails.p_infra(midday, 1)


def test_reregister_needs_afa_before_it_can_debit(world: World):
    """The repaired mandate is not live until the customer authorises it — which is
    exactly why REREGISTER_MANDATE carries a real harm weight."""
    account_id = next(iter(world.accounts))
    action = Action(type=ActionType.REREGISTER_MANDATE, target_rail=Rail.UPI_AUTOPAY)
    mandate = world.rails.register_mandate(
        account_id, Rail.UPI_AUTOPAY, 5_000_000, AT,
        provisional_gate(action, AT, "pol_test"))
    assert mandate.status is MandateStatus.PENDING_AFA
    assert world.rails.mandate_status(mandate.mandate_id) is MandateStatus.PENDING_AFA


def test_every_rail_can_emit_every_cause():
    """Otherwise `code_for` raises mid-batch on a cause the rail has no code for."""
    cm = load_codemap()
    for rail in (Rail.ENACH, Rail.UPI_AUTOPAY, Rail.CARD_EMANDATE):
        for cause in CauseClass:
            if cause is CauseClass.UNKNOWN:
                continue
            assert cm.code_for(rail, cause)


def test_unmapped_codes_are_counted_not_swallowed():
    cm = load_codemap()
    assert cm.cause_of("AP99") is CauseClass.UNKNOWN and cm.is_unmapped("AP99")
    assert not cm.is_unmapped("AP01") and not cm.is_unmapped(SUCCESS)
