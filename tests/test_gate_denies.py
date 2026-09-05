"""M5 acceptance: one test per rule ID, plus the gate's structural guarantees.

A rule with no test is a rule you are hoping about. Every `rule_id` in the catalogue gets
a case that makes it fire and a case that lets it pass, and `test_every_rule_has_a_test`
fails if a rule is ever added without one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    Channel,
    MandateStatus,
    MerchantCategory,
    PTPStatus,
    Rail,
    Verdict,
)
from app.domain.models import (
    Account,
    Action,
    BillingCycle,
    Budgets,
    ConsentState,
    Mandate,
    NoticeReceipt,
    PromiseToPay,
)
from app.policy import (
    RULES,
    RULES_BY_ID,
    AccountFlags,
    Calendar,
    GateContext,
    PolicySet,
    catalogue,
    evaluate,
    rules_hash,
)

NOW = datetime(2026, 9, 10, 11, 0, tzinfo=IST)      # a Thursday, inside quiet hours
AMOUNT = 149900                                      # Rs 1,499 — under every cap


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="module")
def policy(cfg: Config) -> PolicySet:
    return PolicySet.from_config(cfg)


def make_ctx(cfg: Config, *, now: datetime = NOW, notice_at: datetime | None = None,
             notice_consumed: bool = False, notice_amount: int | None = None,
             missing_notice_field: str | None = None, amount: int = AMOUNT,
             category: MerchantCategory = MerchantCategory.SUBSCRIPTION,
             channels: frozenset[Channel] | None = None,
             notice_parts: tuple[int, ...] | None = None,
             attempts_remaining: int = 3, **overrides) -> GateContext:
    """A context that passes every rule, so each test can break exactly one thing."""
    mandate = Mandate(mandate_id="mnd_1", account_id="acc_1", rail=Rail.ENACH,
                      cap_paise=amount * 4, status=MandateStatus.ACTIVE,
                      registered_at=now - timedelta(days=200))
    consent = overrides.pop("consent", None) or ConsentState(
        channels_allowed=channels if channels is not None else frozenset(Channel),
        dnd_registered=False, recording_consent=True,
        afa_authorised_at=now - timedelta(days=10))
    notices: tuple[NoticeReceipt, ...] = ()
    if notice_at is not None:
        fields = {"merchant_name": "Acme", "mandate_reference": "mnd_1",
                  "opt_out_included": True}
        if missing_notice_field == "merchant_name":
            fields["merchant_name"] = ""
        if missing_notice_field == "mandate_reference":
            fields["mandate_reference"] = ""
        if missing_notice_field == "opt_out":
            fields["opt_out_included"] = False
        # One receipt per presentation. A split needs its own notice per part, so the
        # helper has to be able to build several — a single receipt reused would make
        # POL-NOTICE-003 untestable for splits.
        amounts = (notice_parts if notice_parts is not None
                   else (notice_amount if notice_amount is not None else amount,))
        notices = tuple(NoticeReceipt(
            notice_id=f"not_{i + 1}", mandate_id="mnd_1", amount_paise=part,
            issued_at=notice_at, debit_scheduled_for=now,
            consumed_by_action_hash="sha256:spent" if notice_consumed else None,
            **fields) for i, part in enumerate(amounts))
    base = dict(
        now=now,
        account=Account(account_id="acc_1", merchant_category=category, city_tier=1,
                        consent=consent, created_at=now - timedelta(days=300)),
        consent=consent,
        mandates=(mandate,),
        cycle=BillingCycle(cycle_id="cyc_1", account_id="acc_1", amount_paise=amount,
                           due_date=now.date() - timedelta(days=3)),
        budgets=Budgets(attempts_remaining=attempts_remaining,
                        contacts_remaining_week=2,
                        voice_remaining_cycle=1, spend_remaining_paise=8000),
        contacts_made=(),
        notices=notices,
        ptp=None,
        flags=AccountFlags(),
        calendar=Calendar.from_config(cfg),
        cfg=cfg,
    )
    base.update(overrides)
    return GateContext(**base)


def retry(amount: int = AMOUNT, **kw) -> Action:
    return Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=amount,
                  scheduled_for=NOW, **kw)


def message(**kw) -> Action:
    kw.setdefault("template_id", "DLT_RECOVERY_REMIND_001")
    kw.setdefault("channel", Channel.SMS)
    kw.setdefault("cli_series", "1600")
    return Action(type=ActionType.SEND_MESSAGE, **kw)


def voice(**kw) -> Action:
    kw.setdefault("channel", Channel.VOICE)
    kw.setdefault("cli_series", "1600")
    kw.setdefault("disclosure", True)
    return Action(type=ActionType.VOICE_CONFIRM_PTP, **kw)


def notice_ok(cfg: Config, **kw):
    """A retry with a valid 25-hour-old notice behind it."""
    return retry(), make_ctx(cfg, notice_at=NOW - timedelta(hours=25), **kw)


def assert_denied(action, ctx, policy, rule_id: str):
    d = evaluate(action, ctx, policy)
    assert d.verdict is Verdict.DENY, f"expected {rule_id} to deny, got ALLOW"
    assert d.rule_id_failed == rule_id, f"expected {rule_id}, got {d.rule_id_failed}"
    assert d.reason and d.basis, "a denial must name its reason and its basis"
    return d


def assert_allowed(action, ctx, policy):
    d = evaluate(action, ctx, policy)
    assert d.verdict is Verdict.ALLOW, f"denied by {d.rule_id_failed}: {d.reason}"
    return d


# ---- the happy path ------------------------------------------------------------

def test_a_clean_retry_passes_every_rule(cfg, policy):
    action, ctx = notice_ok(cfg)
    d = assert_allowed(action, ctx, policy)
    assert "POL-NOTICE-001" in d.rule_ids_passed
    assert "POL-AFA-001" in d.rule_ids_passed


def test_a_clean_message_passes(cfg, policy):
    assert_allowed(message(), make_ctx(cfg), policy)


# ---- absolute stops -------------------------------------------------------------

def test_POL_STOP_001_opted_out(cfg, policy):
    consent = ConsentState(channels_allowed=frozenset(Channel),
                           opted_out_at=NOW - timedelta(days=1), recording_consent=True,
                           afa_authorised_at=NOW - timedelta(days=5))
    assert_denied(message(), make_ctx(cfg, consent=consent), policy, "POL-STOP-001")


def test_POL_STOP_001_does_not_block_the_mandatory_notice(cfg, policy):
    """An opt-out withdraws consent to *recovery outreach*. The pre-debit notice is a
    regulatory notification attached to a live mandate: suppressing it would block a
    lawful debit rather than protect the customer, and a customer who wants the debits
    to stop revokes the mandate. This gets questioned; that is why it has a test."""
    consent = ConsentState(channels_allowed=frozenset(Channel),
                           opted_out_at=NOW - timedelta(days=1), recording_consent=True,
                           afa_authorised_at=NOW - timedelta(days=5))
    action = Action(type=ActionType.SEND_PREDEBIT_NOTICE, rail=Rail.ENACH,
                    amount_paise=AMOUNT, scheduled_for=NOW + timedelta(hours=25),
                    channel=Channel.SMS, template_id="DLT_RECOVERY_NOTICE_001",
                    cli_series="1600")
    assert_allowed(action, make_ctx(cfg, consent=consent), policy)


def test_POL_STOP_002_disputed(cfg, policy):
    ctx = make_ctx(cfg, flags=AccountFlags(disputed=True))
    assert_denied(message(), ctx, policy, "POL-STOP-002")
    assert_allowed(Action(type=ActionType.ESCALATE_HUMAN), ctx, policy)
    assert_allowed(Action(type=ActionType.CLOSE), ctx, policy)


def test_POL_STOP_003_subjudice(cfg, policy):
    ctx = make_ctx(cfg, flags=AccountFlags(subjudice=True))
    assert_denied(message(), ctx, policy, "POL-STOP-003")
    # the ways *out* stay open: stopping recovery is not the same as abandoning the file
    assert_allowed(Action(type=ActionType.ESCALATE_HUMAN), ctx, policy)
    assert_allowed(Action(type=ActionType.WAIT), ctx, policy)


def test_POL_STOP_004_bereavement(cfg, policy):
    recent = make_ctx(cfg, flags=AccountFlags(bereavement_at=NOW - timedelta(days=5)))
    assert_denied(message(), recent, policy, "POL-STOP-004")
    old = make_ctx(cfg, flags=AccountFlags(bereavement_at=NOW - timedelta(days=40)))
    assert_allowed(message(), old, policy)


def test_POL_STOP_005_terminal_state_written(cfg, policy):
    ctx = make_ctx(cfg, flags=AccountFlags(terminal_state="RECOVERED"))
    assert_denied(Action(type=ActionType.WAIT), ctx, policy, "POL-STOP-005")


# ---- contact timing --------------------------------------------------------------

def test_POL_QH_001_quiet_hours(cfg, policy):
    """The demo's live denial: a voice call at 19:30."""
    evening = NOW.replace(hour=19, minute=30)
    d = assert_denied(voice(), make_ctx(cfg, now=evening), policy, "POL-QH-001")
    assert "08:00" in d.reason and "19:00" in d.reason
    assert "8am-7pm" in d.basis
    assert_allowed(voice(), make_ctx(cfg, now=NOW.replace(hour=18, minute=59)), policy)
    assert_denied(voice(), make_ctx(cfg, now=NOW.replace(hour=7, minute=59)), policy,
                  "POL-QH-001")


def test_POL_QH_001_exempts_the_predebit_notice(cfg, policy):
    """A regulatory notification, not a recovery contact. Encoded explicitly because it
    is the kind of nuance that shows the rules were read rather than pattern-matched."""
    action = Action(type=ActionType.SEND_PREDEBIT_NOTICE, rail=Rail.ENACH,
                    amount_paise=AMOUNT, scheduled_for=NOW + timedelta(hours=25),
                    channel=Channel.SMS, template_id="DLT_RECOVERY_NOTICE_001",
                    cli_series="1600")
    assert_allowed(action, make_ctx(cfg, now=NOW.replace(hour=22)), policy)


def test_POL_QH_002_no_voice_on_sundays_or_holidays(cfg, policy):
    sunday = datetime(2026, 9, 13, 11, 0, tzinfo=IST)
    assert sunday.weekday() == 6
    assert_denied(voice(), make_ctx(cfg, now=sunday), policy, "POL-QH-002")
    holiday = datetime(2026, 1, 26, 11, 0, tzinfo=IST)
    assert_denied(voice(), make_ctx(cfg, now=holiday), policy, "POL-QH-002")
    # a message on the same Sunday is fine — the rule is voice-only
    assert_allowed(message(), make_ctx(cfg, now=sunday), policy)


def test_POL_QH_003_festivals(cfg, policy):
    festival = datetime(2026, 9, 14, 11, 0, tzinfo=IST)
    assert festival.date() in Calendar.from_config(cfg).festivals
    assert_denied(message(), make_ctx(cfg, now=festival), policy, "POL-QH-003")


# ---- debit rules -----------------------------------------------------------------

def test_POL_NOTICE_001_requires_a_notice(cfg, policy):
    assert_denied(retry(), make_ctx(cfg), policy, "POL-NOTICE-001")


def test_POL_NOTICE_001_requires_a_full_24_hours(cfg, policy):
    assert_denied(retry(), make_ctx(cfg, notice_at=NOW - timedelta(hours=23)), policy,
                  "POL-NOTICE-001")
    assert_allowed(retry(), make_ctx(cfg, notice_at=NOW - timedelta(hours=24)), policy)


def test_POL_NOTICE_001_matches_on_amount(cfg, policy):
    """A notice announcing Rs 1,499 does not authorise a debit of Rs 4,999."""
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25), notice_amount=499900)
    assert_denied(retry(), ctx, policy, "POL-NOTICE-001")


@pytest.mark.parametrize("field", ["merchant_name", "mandate_reference", "opt_out"])
def test_POL_NOTICE_002_mandated_fields(cfg, policy, field):
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25), missing_notice_field=field)
    d = evaluate(retry(), ctx, policy)
    assert d.verdict is Verdict.DENY
    assert d.rule_id_failed == "POL-NOTICE-002"


def test_POL_NOTICE_003_one_notice_one_attempt(cfg, policy):
    """The load-bearing assumption. A spent notice does not authorise a second debit."""
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25), notice_consumed=True)
    d = evaluate(retry(), ctx, policy)
    assert d.verdict is Verdict.DENY
    assert d.rule_id_failed == "POL-NOTICE-003"


def test_POL_AFA_001_ceiling(cfg, policy):
    over = 2_000_000        # Rs 20,000, above the Rs 15,000 general ceiling
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25), amount=over)
    assert_denied(retry(over), ctx, policy, "POL-AFA-001")


def test_POL_AFA_001_high_cap_categories(cfg, policy):
    """Insurance, mutual funds and credit-card bills get the Rs 1,00,000 ceiling."""
    over = 2_000_000
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25), amount=over,
                   category=MerchantCategory.INSURANCE)
    assert_allowed(retry(over), ctx, policy)


def test_POL_AFA_002_reregister_needs_fresh_authorisation(cfg, policy):
    """docs/04 lists re-registering without fresh consent as explicitly wrong for a
    revoked mandate. This is the rule that makes 're-consent, then repair' a real
    sequence rather than a label."""
    action = Action(type=ActionType.REREGISTER_MANDATE, target_rail=Rail.UPI_AUTOPAY)
    stale = ConsentState(channels_allowed=frozenset(Channel), recording_consent=True,
                         afa_authorised_at=NOW - timedelta(days=200))
    assert_denied(action, make_ctx(cfg, consent=stale), policy, "POL-AFA-002")
    none = ConsentState(channels_allowed=frozenset(Channel), recording_consent=True)
    assert_denied(action, make_ctx(cfg, consent=none), policy, "POL-AFA-002")
    assert_allowed(action, make_ctx(cfg), policy)


def test_POL_AMT_001_amount_must_reconcile(cfg, policy):
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25))
    bad = Action(type=ActionType.RETRY_DEBIT, rail=Rail.ENACH, amount_paise=AMOUNT + 1,
                 scheduled_for=NOW)
    d = evaluate(bad, ctx, policy)
    assert d.verdict is Verdict.DENY
    assert d.rule_id_failed in ("POL-NOTICE-001", "POL-AMT-001")


def test_POL_AMT_001_split_must_sum_to_the_cycle(cfg, policy):
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25))
    good = Action(type=ActionType.SPLIT_DEBIT, rail=Rail.ENACH, amount_paise=AMOUNT,
                  scheduled_for=NOW, parts=(74950, 74950))
    bad = Action(type=ActionType.SPLIT_DEBIT, rail=Rail.ENACH, amount_paise=AMOUNT,
                 scheduled_for=NOW, parts=(74950, 10000))
    assert sum(good.parts) == AMOUNT
    assert evaluate(bad, ctx, policy).rule_id_failed in ("POL-NOTICE-001", "POL-AMT-001")


# ---- channel and messaging --------------------------------------------------------

def test_POL_CONSENT_001_channel_not_consented(cfg, policy):
    ctx = make_ctx(cfg, channels=frozenset({Channel.EMAIL}))
    assert_denied(message(channel=Channel.SMS), ctx, policy, "POL-CONSENT-001")
    assert_allowed(message(channel=Channel.EMAIL), ctx, policy)


def test_POL_DLT_001_template_must_be_registered(cfg, policy):
    assert_denied(message(template_id="MY_OWN_TEMPLATE"), make_ctx(cfg), policy,
                  "POL-DLT-001")


def test_POL_DLT_002_no_free_text(cfg, policy):
    action = Action(type=ActionType.SEND_MESSAGE, channel=Channel.SMS,
                    cli_series="1600", template_id=None)
    d = evaluate(action, make_ctx(cfg), policy)
    assert d.verdict is Verdict.DENY
    assert d.rule_id_failed in ("POL-DLT-001", "POL-DLT-002")


def test_action_has_no_message_body_field():
    """The structural half of POL-DLT-002: there is nowhere to put free text, so no
    future edit can smuggle a composed message past the gate."""
    fields = set(Action.__dataclass_fields__)
    assert not fields & {"body", "message", "message_body", "text", "content", "phone"}


def test_POL_NUM_001_number_series(cfg, policy):
    assert_denied(message(cli_series="140"), make_ctx(cfg), policy, "POL-NUM-001")
    assert_denied(message(cli_series=None), make_ctx(cfg), policy, "POL-NUM-001")
    promo = message(cli_series="140", promotional=True)
    dnd = ConsentState(channels_allowed=frozenset(Channel), dnd_registered=True,
                       recording_consent=True, afa_authorised_at=NOW)
    assert_denied(promo, make_ctx(cfg, consent=dnd), policy, "POL-NUM-001")
    assert_allowed(promo, make_ctx(cfg), policy)


def test_POL_PURPOSE_001_purpose_limitation(cfg, policy):
    other = ConsentState(channels_allowed=frozenset(Channel), recording_consent=True,
                         purpose="marketing", afa_authorised_at=NOW)
    assert_denied(message(), make_ctx(cfg, consent=other), policy, "POL-PURPOSE-001")


# ---- frequency, fatigue and budget -------------------------------------------------

def test_POL_FREQ_001_weekly_cap(cfg, policy):
    contacts = tuple(NOW - timedelta(days=d) for d in (1, 2, 3))
    assert_denied(message(), make_ctx(cfg, contacts_made=contacts), policy,
                  "POL-FREQ-001")
    # contacts older than the rolling window do not count
    old = tuple(NOW - timedelta(days=d) for d in (8, 9, 10))
    assert_allowed(message(), make_ctx(cfg, contacts_made=old), policy)


def test_POL_FREQ_002_daily_cap(cfg, policy):
    today = (NOW - timedelta(hours=2),)
    assert_denied(message(), make_ctx(cfg, contacts_made=today), policy, "POL-FREQ-002")


def test_POL_FREQ_003_voice_budget(cfg, policy):
    spent = Budgets(attempts_remaining=3, contacts_remaining_week=2,
                    voice_remaining_cycle=0, spend_remaining_paise=8000)
    assert_denied(voice(), make_ctx(cfg, budgets=spent), policy, "POL-FREQ-003")


def test_POL_BUDGET_001_attempts(cfg, policy):
    spent = Budgets(attempts_remaining=0, contacts_remaining_week=2,
                    voice_remaining_cycle=1, spend_remaining_paise=8000)
    ctx = make_ctx(cfg, notice_at=NOW - timedelta(hours=25), budgets=spent)
    assert_denied(retry(), ctx, policy, "POL-BUDGET-001")


def test_POL_BUDGET_002_spend(cfg, policy):
    broke = Budgets(attempts_remaining=3, contacts_remaining_week=2,
                    voice_remaining_cycle=1, spend_remaining_paise=5)
    assert_denied(message(), make_ctx(cfg, budgets=broke), policy, "POL-BUDGET-002")


def test_POL_PTP_001_open_promise_suppresses_everything_but_wait(cfg, policy):
    ptp = PromiseToPay(ptp_id="ptp_1", account_id="acc_1", cycle_id="cyc_1",
                       amount_paise=AMOUNT, promised_date=NOW.date() + timedelta(days=4),
                       channel=Channel.SMS, captured_by="REQUEST_PTP", confidence=0.6,
                       status=PTPStatus.OPEN)
    ctx = make_ctx(cfg, ptp=ptp)
    assert_denied(message(), ctx, policy, "POL-PTP-001")
    assert_allowed(Action(type=ActionType.WAIT), ctx, policy)


def test_POL_PTP_001_releases_after_the_grace_period(cfg, policy):
    ptp = PromiseToPay(ptp_id="ptp_1", account_id="acc_1", cycle_id="cyc_1",
                       amount_paise=AMOUNT, promised_date=NOW.date() - timedelta(days=3),
                       channel=Channel.SMS, captured_by="REQUEST_PTP", confidence=0.6,
                       status=PTPStatus.OPEN)
    assert_allowed(message(), make_ctx(cfg, ptp=ptp), policy)


# ---- AI-specific --------------------------------------------------------------------

def test_POL_AI_001_disclosure(cfg, policy):
    assert_denied(voice(disclosure=False), make_ctx(cfg), policy, "POL-AI-001")


def test_POL_AI_002_recording_consent(cfg, policy):
    no_recording = ConsentState(channels_allowed=frozenset(Channel),
                                recording_consent=False, afa_authorised_at=NOW)
    assert_denied(voice(), make_ctx(cfg, consent=no_recording), policy, "POL-AI-002")


def test_POL_AI_003_human_override_still_passes_the_gate(cfg, policy):
    """FREE-AI: accountability regardless of autonomy. A human can override the agent's
    *choice*; a human cannot override compliance. The overriding human's 19:30 voice call
    is refused exactly like the agent's would be."""
    evening = make_ctx(cfg, now=NOW.replace(hour=19, minute=30))
    assert_denied(voice(), evening, policy, "POL-QH-001")
    assert "POL-AI-003" in [r.rule_id for r in RULES]


def test_POL_AI_004_no_re_automation_after_escalation(cfg, policy):
    ctx = make_ctx(cfg, flags=AccountFlags(escalated_this_cycle=True))
    assert_denied(message(), ctx, policy, "POL-AI-004")
    assert_allowed(Action(type=ActionType.CLOSE), ctx, policy)
    assert_allowed(Action(type=ActionType.WAIT), ctx, policy)


# ---- structural guarantees ------------------------------------------------------------

def test_gate_is_pure(cfg, policy):
    """Same inputs, same output. No clock read, no RNG, no I/O — which is what makes a
    denial reproducible months later."""
    action, ctx = notice_ok(cfg)
    first = evaluate(action, ctx, policy)
    second = evaluate(action, ctx, policy)
    assert (first.verdict, first.rule_ids_passed, first.rule_id_failed) == \
           (second.verdict, second.rule_ids_passed, second.rule_id_failed)


def test_gate_reads_no_real_time():
    import inspect
    import app.policy.gate as gate_mod
    import app.policy.rules as rules_mod
    for mod in (gate_mod, rules_mod):
        src = inspect.getsource(mod)
        assert "datetime.now(" not in src and "date.today(" not in src


def test_first_deny_short_circuits(cfg, policy):
    """Later rules are not evaluated, and rule_ids_passed holds exactly what was checked
    before the failure."""
    ctx = make_ctx(cfg, flags=AccountFlags(disputed=True))
    d = evaluate(message(), ctx, policy)
    assert d.rule_id_failed == "POL-STOP-002"
    assert d.rule_ids_passed == ("POL-STOP-001",)
    assert "POL-QH-001" not in d.rule_ids_passed


def test_all_actions_covered():
    """Every ActionType is matched by at least one rule, so nothing slips through
    unexamined."""
    covered = set().union(*(r.applies_to for r in RULES))
    assert set(ActionType) - covered == set()


def test_every_rule_has_a_test():
    """A rule with no test is a rule you are hoping about."""
    source = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    missing = [r.rule_id for r in RULES
               if f"test_{r.rule_id.replace('-', '_')}" not in source]
    assert not missing, f"rules with no test: {missing}"


def test_every_rule_names_its_basis():
    """A denial that cannot point at which regulation produced it is an opinion."""
    for r in RULES:
        assert r.basis and len(r.basis) > 10, r.rule_id
        assert r.deny_reason and len(r.deny_reason) > 10, r.rule_id


def test_policy_version_bumps(cfg):
    """CI fails if a rule changed without the declared version and hash moving with it.
    A trail that cannot name the policy in force at the time is not a defence."""
    assert cfg.raw["rules_hash"] == rules_hash(), (
        "The rule set has changed but config/default.yaml still declares the old hash. "
        f"Bump policy_version and set rules_hash to {rules_hash()}")


def test_rule_ids_are_unique():
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids))


def test_catalogue_is_serialisable():
    """GET /policy/rules — id, title, basis, applies_to."""
    entries = catalogue()
    assert len(entries) == len(RULES)
    assert all({"rule_id", "title", "basis", "applies_to"} <= set(e) for e in entries)


# ---- the live-denial endpoint --------------------------------------------------------

def test_dry_run_denial_is_written_to_the_ledger(tmp_path, cfg):
    """The demo's own denial appears in the audit trail it then shows."""
    import sqlite3
    from app.policy.evaluate_api import evaluate_hypothetical
    from app.sim.generate import simulate_batch

    path = str(tmp_path / "dry.db")
    world, _ = simulate_batch(cfg, seed=42, n_accounts=20, out_path=path)
    account_id = next(iter(world.accounts))
    evening = datetime(2026, 9, 10, 19, 30, tzinfo=IST)

    out = evaluate_hypothetical(path, account_id, voice(), evening, cfg)
    assert out["verdict"] == "DENY"
    assert out["rule_id_failed"] == "POL-QH-001"
    assert "8am-7pm" in out["basis"]

    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT rule_failed, payload FROM events WHERE stage='GATE' AND account_id=?",
        (account_id,)).fetchone()
    con.close()
    assert row[0] == "POL-QH-001"
    assert __import__("json").loads(row[1])["dry_run"] is True


def test_a_dry_run_allow_authorises_nothing(tmp_path, cfg):
    """A verdict minted by the demo endpoint must not look to the verifier like one that
    came through the runner. Without this, /policy/evaluate would be a way to mint
    executable ALLOWs."""
    import json
    import sqlite3
    from app.domain.enums import Arm, Stage as S
    from app.ledger import EventDraft, Ledger
    from app.policy.evaluate_api import evaluate_hypothetical
    from app.sim.generate import simulate_batch

    path = str(tmp_path / "mint.db")
    world, batch_id = simulate_batch(cfg, seed=42, n_accounts=5, out_path=path)
    account_id = next(iter(world.accounts))
    noon = datetime(2026, 9, 10, 12, 0, tzinfo=IST)
    action = message()
    out = evaluate_hypothetical(path, account_id, action, noon, cfg)
    assert out["verdict"] == "ALLOW"

    # try to execute on the back of that hypothetical ALLOW
    led = Ledger(path, batch_id)
    led.append(EventDraft(stage=S.ASSIGN, occurred_at=noon, account_id=account_id,
                          arm=Arm.TREATMENT))
    led.append(EventDraft(
        stage=S.EXECUTE, occurred_at=noon, account_id=account_id,
        decision_id="dec_dryrun", action={"type": "SEND_MESSAGE"},
        action_hash=action.hash(), policy={"version": cfg.policy_version}))
    rep = led.verify()
    led.close()
    assert not rep.ok
    assert any("no matching ALLOW gate" in f for f in rep.failures)


def test_dry_runs_do_not_disturb_the_account_ordering_invariant(tmp_path, cfg):
    """A hypothetical may be evaluated before the arm is assigned — it reads no arm and
    changes nothing — and `verify()` must still pass."""
    from app.policy.evaluate_api import evaluate_hypothetical
    from app.ledger import Ledger
    from app.sim.generate import simulate_batch

    path = str(tmp_path / "order.db")
    world, batch_id = simulate_batch(cfg, seed=42, n_accounts=5, out_path=path)
    account_id = next(iter(world.accounts))
    evaluate_hypothetical(path, account_id, voice(),
                          datetime(2026, 9, 10, 19, 30, tzinfo=IST), cfg)
    led = Ledger(path, batch_id)
    rep = led.verify()
    led.close()
    assert rep.ok, rep.failures
