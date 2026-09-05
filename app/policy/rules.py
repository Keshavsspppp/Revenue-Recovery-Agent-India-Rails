"""The rule catalogue. docs/05-POLICY-ENGINE.md.

"You must not call customers after 7pm" in a system prompt is a suggestion. It holds
ninety-nine times and fails on the hundredth, and the hundredth is the one on the
recording. Every rule here is a pure predicate over (action, context), evaluated before
anything reaches a rail, and its verdict is written to the ledger whether it passed or
failed.

Each rule carries the regulation it implements in `basis`, so a denial can point at *which
rule of the real world* produced it — the difference between "we thought about compliance"
and "compliance is in the control loop".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from app.domain.enums import (
    CONTACT_ACTIONS,
    DEBIT_ACTIONS,
    ActionType,
    PTPStatus,
)
from app.domain.models import Action, sha256_of

ALL_ACTIONS = frozenset(ActionType)

#: Contact actions plus the pre-debit notice. The notice is a regulatory notification
#: rather than recovery outreach, so it is exempt from the quiet-hours and opt-out rules
#: below — but it still lands on a customer's phone, so consent, DLT registration and
#: purpose limitation all still apply to it.
OUTREACH = CONTACT_ACTIONS | {ActionType.SEND_PREDEBIT_NOTICE}

#: Actions that pursue money from the customer. Everything except the ways out.
RECOVERY_ACTIONS = ALL_ACTIONS - {ActionType.WAIT, ActionType.CLOSE,
                                  ActionType.ESCALATE_HUMAN,
                                  ActionType.OFFER_ACCOMMODATION}

#: Customer-facing messages whose body must come from a registered template.
TEMPLATED = frozenset({ActionType.SEND_MESSAGE, ActionType.SEND_PAYMENT_LINK,
                       ActionType.REQUEST_PTP, ActionType.SEND_PREDEBIT_NOTICE})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    basis: str                 # the regulation or policy it implements
    applies_to: frozenset[ActionType]
    check: Callable[[Action, "GateContext"], bool]   # noqa: F821 — set at call time
    deny_reason: str

    def identity(self) -> dict[str, object]:
        """What the policy version hashes over. Deliberately excludes `check`: a Python
        function has no stable hash, so the version pins the *declared* rule set and the
        per-rule tests pin the behaviour."""
        return {"rule_id": self.rule_id, "title": self.title, "basis": self.basis,
                "applies_to": sorted(a.value for a in self.applies_to),
                "deny_reason": self.deny_reason}


# ---- helpers used by several rules ---------------------------------------------

def _mandate_for(action: Action, ctx) -> object | None:
    rail = action.rail or action.target_rail
    for m in ctx.mandates:
        if m.rail is rail:
            return m
    return None


def presentations(action: Action) -> tuple[int, ...]:
    """The amounts this action actually presents to the rail.

    One for an ordinary debit; one per part for a split. Every per-presentation rule —
    the AFA-free ceiling, the notice coupling, the attempt budget — reads this rather
    than `amount_paise`, because a split's `amount_paise` is the cycle total and none of
    those limits are per cycle.
    """
    if action.type is ActionType.SPLIT_DEBIT and action.parts:
        return tuple(action.parts)
    return (action.amount_paise or 0,)


def _match_notices(action: Action, ctx, *, unspent: bool):
    """One receipt per presentation, or None if any of them is missing.

    `unspent` decides whether an already-consumed receipt counts. That distinction is the
    whole reason POL-NOTICE-001 and -003 are separate rules: both used to ask for an
    unspent notice, so -001 always denied first with the same predicate and -003 could
    never fire in any batch — a consumed notice was reported as "no notice was issued",
    which is a different compliance failure with a different fix.

    A split needs its own receipt per part. Each part is a separate presentation, and
    POL-NOTICE-003 is one notice per presentation — so two equal parts need two distinct
    receipts, not one receipt used twice.
    """
    mandate = _mandate_for(action, ctx)
    if mandate is None:
        return None
    hours = ctx.notice_hours
    due = action.scheduled_for or ctx.now
    pool = [r for r in ctx.notices
            if r.mandate_id == mandate.mandate_id
            and (r.consumed_by_action_hash is None or not unspent)
            and due - r.issued_at >= timedelta(hours=hours)]
    matched: list = []
    for amount in presentations(action):
        for receipt in pool:
            if receipt.amount_paise == amount and receipt not in matched:
                matched.append(receipt)
                break
        else:
            return None
    return tuple(matched)


def _matching_notice(action: Action, ctx, *, unspent: bool):
    """The first receipt covering this action, spent or not per `unspent`."""
    matched = _match_notices(action, ctx, unspent=unspent)
    return matched[0] if matched else None


def _live_notice(action: Action, ctx):
    """The receipts that would actually authorise this debit — all unspent."""
    return _match_notices(action, ctx, unspent=True)


def _contacts_within(ctx, days: int) -> int:
    cutoff = ctx.now - timedelta(days=days)
    return sum(1 for c in ctx.contacts_made if c > cutoff)


# ---- the catalogue --------------------------------------------------------------
# Order matters. Absolute stops are evaluated first and deny everything; the first DENY
# short-circuits, and every rule evaluated before it is recorded in `rule_ids_passed`.

RULES: tuple[Rule, ...] = (

    # ---- absolute stops ---------------------------------------------------------
    Rule("POL-STOP-001", "Opted-out customers receive no recovery outreach",
         "TCCCPR / DPDP — withdrawal of consent",
         CONTACT_ACTIONS,
         lambda a, ctx: ctx.consent.opted_out_at is None,
         "The customer has opted out of recovery contact"),

    Rule("POL-STOP-002", "A disputed account is human-only",
         "RBI conduct norms — disputes are not automated",
         ALL_ACTIONS - {ActionType.ESCALATE_HUMAN, ActionType.CLOSE},
         lambda a, ctx: not ctx.flags.disputed,
         "The account is disputed; only escalation or closure is permitted"),

    Rule("POL-STOP-003", "No recovery activity while the matter is subjudice",
         "RBI draft recovery norms — no recovery while subjudice",
         RECOVERY_ACTIONS,
         lambda a, ctx: not ctx.flags.subjudice,
         "The matter is subjudice; recovery activity is suspended"),

    Rule("POL-STOP-004", "No contact during bereavement or family calamity",
         "RBI draft recovery norms — bereavement and calamity suppression",
         OUTREACH,
         lambda a, ctx: (ctx.flags.bereavement_at is None
                         or ctx.now - ctx.flags.bereavement_at
                         > timedelta(days=ctx.bereavement_days)),
         "Contact is suppressed following a bereavement"),

    Rule("POL-STOP-005", "Nothing follows a terminal state",
         "ledger invariant 6 — one terminal state per cycle",
         ALL_ACTIONS,
         lambda a, ctx: ctx.flags.terminal_state is None,
         "This cycle already has a terminal state"),

    # ---- contact timing ---------------------------------------------------------
    Rule("POL-QH-001", "Contact only between 08:00 and 19:00 IST",
         "RBI draft recovery norms — 8am-7pm calling window",
         # SEND_PREDEBIT_NOTICE is deliberately absent. It is a regulatory notification
         # the merchant is *required* to send before a debit, not a recovery contact, and
         # suppressing it would block a lawful debit rather than protect the customer.
         # This exemption gets questioned; that is the point of writing it down.
         CONTACT_ACTIONS,
         lambda a, ctx: ctx.quiet_start <= ctx.now.hour < ctx.quiet_end,
         "Contact actions are permitted only between 08:00 and 19:00 IST"),

    Rule("POL-QH-002", "No automated voice on Sundays or gazetted holidays",
         "conservative reading of 'decency and decorum'",
         frozenset({ActionType.VOICE_CONFIRM_PTP}),
         lambda a, ctx: (ctx.voice_allow_holidays
                         or (ctx.now.weekday() != 6
                             and ctx.now.date() not in ctx.calendar.holidays)),
         "Automated voice calls are not placed on Sundays or gazetted holidays"),

    Rule("POL-QH-003", "No contact on festival dates",
         "RBI draft recovery norms — no contact during festivals",
         OUTREACH,
         lambda a, ctx: ctx.now.date() not in ctx.calendar.festivals,
         "Contact is suppressed on festival dates"),

    # ---- debit rules ------------------------------------------------------------
    Rule("POL-NOTICE-001", "A debit requires a pre-debit notice at least 24h earlier",
         "RBI Digital Payments E-Mandate Framework — pre-debit notification",
         DEBIT_ACTIONS,
         lambda a, ctx: _match_notices(a, ctx, unspent=False) is not None,
         "No pre-debit notice was issued at least 24 hours before this debit"),

    Rule("POL-NOTICE-002", "The notice must carry every mandated field",
         "RBI e-mandate framework — merchant, amount, date, mandate reference, opt-out",
         DEBIT_ACTIONS,
         lambda a, ctx: (lambda ns: ns is not None and all(
                             bool(n.merchant_name) and n.amount_paise > 0
                             and n.debit_scheduled_for is not None
                             and bool(n.mandate_reference) and n.opt_out_included
                             for n in ns)
                         )(_match_notices(a, ctx, unspent=False)),
         "The pre-debit notice is missing a mandated field"),

    Rule("POL-NOTICE-003", "One notice authorises one attempt",
         "RBI e-mandate framework — fresh notice per presentation (see docs/DECISIONS.md)",
         DEBIT_ACTIONS,
         lambda a, ctx: _live_notice(a, ctx) is not None,
         "The available notice has already been consumed by an earlier attempt"),

    Rule("POL-AFA-001", "Unattended debits stay under the AFA-free ceiling",
         "RBI e-mandate — Rs 15,000 general / Rs 1,00,000 for approved categories",
         DEBIT_ACTIONS,
         # Per *presentation*, which is what makes a split a remedy rather than a
         # relabelled retry: two debits of Rs 10,000 each clear a Rs 15,000 ceiling that
         # one of Rs 20,000 does not. Reading `amount_paise` here would have denied every
         # split on the cycle total it reconciles to.
         lambda a, ctx: (ctx.afa_free_cap is None
                         or max(presentations(a)) <= ctx.afa_free_cap),
         "The amount exceeds the additional-factor-free ceiling for this category"),

    Rule("POL-AFA-002", "A mandate is not moved without fresh customer authorisation",
         "RBI e-mandate — AFA on registration, modification and withdrawal",
         frozenset({ActionType.REREGISTER_MANDATE}),
         lambda a, ctx: (ctx.consent.afa_authorised_at is not None
                         and ctx.now - ctx.consent.afa_authorised_at
                         <= timedelta(days=ctx.afa_freshness_days)),
         "Re-registering a mandate requires fresh customer authorisation"),

    Rule("POL-AMT-001", "A debit collects the cycle amount, no more and no less",
         "internal integrity",
         DEBIT_ACTIONS,
         lambda a, ctx: (sum(a.parts) == ctx.cycle.amount_paise
                         if a.type is ActionType.SPLIT_DEBIT and a.parts
                         else a.amount_paise == ctx.cycle.amount_paise),
         "The debit amount does not reconcile to the cycle amount"),

    # ---- channel and messaging --------------------------------------------------
    Rule("POL-CONSENT-001", "Only channels the customer consented to",
         "DPDP — consent",
         OUTREACH,
         lambda a, ctx: (a.channel is None
                         or a.channel in ctx.consent.channels_allowed),
         "The customer has not consented to this channel"),

    Rule("POL-DLT-001", "Messages use a registered DLT template",
         "TRAI TCCCPR — DLT sender, header and template registration",
         TEMPLATED,
         lambda a, ctx: a.template_id in ctx.dlt_templates,
         "The template is not registered on the DLT platform"),

    Rule("POL-DLT-002", "No free-text customer messaging",
         "TRAI TCCCPR + README invariant 4 — bodies are rendered, never composed",
         TEMPLATED,
         lambda a, ctx: a.template_id is not None,
         "Customer-facing text must be rendered from a registered template"),

    Rule("POL-NUM-001", "Service traffic uses the 1600 series; promotional uses 140",
         "TRAI clarification on the 1600 and 140 number series",
         OUTREACH,
         lambda a, ctx: (
             (a.cli_series == ctx.cli_series_promotional and not ctx.consent.dnd_registered)
             if a.promotional else a.cli_series == ctx.cli_series_service),
         "Wrong number series for this traffic class, or promotional traffic to a DND "
         "registered customer"),

    Rule("POL-PURPOSE-001", "Data is used only for the purpose it was collected for",
         "DPDP — purpose limitation",
         ALL_ACTIONS,
         lambda a, ctx: ctx.consent.purpose == "payment_recovery",
         "The consent on file does not cover payment recovery"),

    # ---- frequency, fatigue and budget ------------------------------------------
    Rule("POL-FREQ-001", "At most three contacts in any rolling seven days",
         "RBI draft recovery norms — no excessive calling",
         CONTACT_ACTIONS,
         lambda a, ctx: _contacts_within(ctx, 7) < ctx.max_contacts_per_week,
         "The weekly contact cap for this account is spent"),

    Rule("POL-FREQ-002", "At most one contact per calendar day",
         "internal conduct policy — stricter than the RBI draft's weekly cap, and ours "
         "to justify rather than a regulator's",
         CONTACT_ACTIONS,
         lambda a, ctx: sum(1 for c in ctx.contacts_made
                            if c.date() == ctx.now.date()) < ctx.max_contacts_per_day,
         "This account has already been contacted today"),

    Rule("POL-FREQ-003", "At most one automated voice call per cycle",
         "internal conduct policy — voice is the most intrusive action in the set and "
         "is priced accordingly in harm_weights",
         frozenset({ActionType.VOICE_CONFIRM_PTP}),
         lambda a, ctx: ctx.budgets.voice_remaining_cycle > 0,
         "The voice budget for this cycle is spent"),

    Rule("POL-BUDGET-001", "A debit needs an attempt left in the budget",
         "internal — attempts are scarce because each is notice-gated",
         DEBIT_ACTIONS,
         lambda a, ctx: ctx.budgets.attempts_remaining >= len(presentations(a)),
         "No attempts remain in this cycle's budget"),

    Rule("POL-BUDGET-002", "An action must fit the remaining spend",
         "internal — a per-account spend cap bounds the blast radius of a wrong decision",
         ALL_ACTIONS,
         lambda a, ctx: ctx.action_cost(a) <= ctx.budgets.spend_remaining_paise,
         "The action costs more than the remaining budget for this account"),

    Rule("POL-PTP-001", "An open promise suppresses everything except waiting",
         "conduct + design — a promise is a commitment, so honour it",
         ALL_ACTIONS - {ActionType.WAIT},
         lambda a, ctx: (ctx.ptp is None or ctx.ptp.status is not PTPStatus.OPEN
                         or ctx.now.date() > ctx.ptp.promised_date
                         + timedelta(days=ctx.ptp_grace_days)),
         "An open promise to pay suppresses contact until it falls due"),

    # ---- AI-specific ------------------------------------------------------------
    Rule("POL-AI-001", "An automated call discloses that it is automated",
         "RBI FREE-AI — disclosure and the right to override",
         frozenset({ActionType.VOICE_CONFIRM_PTP}),
         lambda a, ctx: a.disclosure is True,
         "An automated call must open by identifying itself and offering an opt-out"),

    Rule("POL-AI-002", "Calls are recorded with prior intimation and consent",
         "RBI draft recovery norms — recorded with prior intimation",
         frozenset({ActionType.VOICE_CONFIRM_PTP}),
         lambda a, ctx: ctx.consent.recording_consent,
         "The customer has not consented to call recording"),

    Rule("POL-AI-003", "A human may override the agent, never compliance",
         "RBI FREE-AI — accountability regardless of autonomy",
         ALL_ACTIONS,
         # Deliberately always True. A human override skips proposer *selection* but is
         # handed to this same gate like anything else, so every rule above still binds.
         # The rule exists so the trail records that the override was evaluated, not
         # waved through. tests/test_gate_denies.py pins the behaviour.
         lambda a, ctx: True,
         "A human override does not bypass the compliance gate"),

    Rule("POL-AI-004", "An escalated account does not re-enter automation this cycle",
         "conduct — no escalate-then-auto-close loops",
         ALL_ACTIONS - {ActionType.CLOSE, ActionType.WAIT},
         lambda a, ctx: not ctx.flags.escalated_this_cycle,
         "This account was escalated to a human and cannot be re-automated this cycle"),
)


RULES_BY_ID: dict[str, Rule] = {r.rule_id: r for r in RULES}


def rules_hash(rules: tuple[Rule, ...] = RULES) -> str:
    """A hash over the declared rule set. `policy_version` in config must match it, so
    changing a rule without bumping the version fails CI — a trail that cannot name the
    policy in force at the time is not a defence."""
    return sha256_of([r.identity() for r in rules])
