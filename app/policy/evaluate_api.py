"""Evaluate a hypothetical action against the real gate, executing nothing.

This is what `POST /policy/evaluate` calls, and it is built for one purpose: setting the
clock to 19:30 on stage, submitting a voice call, and watching the gate refuse it —
then showing the same denial written in the ledger.

It builds its context from the batch database, so the denial is against a real account's
real state, not a mock. Nothing is executed: the action never reaches an adapter.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.clock import IST
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    Channel,
    MandateStatus,
    MerchantCategory,
    Rail,
    Stage,
)
from app.domain.models import (
    Account,
    Action,
    BillingCycle,
    Budgets,
    ConsentState,
    Mandate,
)
from app.ledger import EventDraft, Ledger
from app.policy.gate import AccountFlags, Calendar, GateContext, PolicySet, evaluate


class UnknownAccount(Exception):
    pass


def context_for(path: str | Path, account_id: str, at: datetime,
                cfg: Config) -> GateContext:
    """Rebuild an account's compliance context from the batch database, as of `at`."""
    if at.tzinfo is None:          # the rules are all IST; a bare wall clock means IST
        at = at.replace(tzinfo=IST)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    account = con.execute(
        "SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
    if account is None:
        con.close()
        raise UnknownAccount(f"no such account in this batch: {account_id}")
    cycle = con.execute(
        "SELECT * FROM cycles WHERE account_id=?", (account_id,)).fetchone()
    mandates = con.execute(
        "SELECT * FROM mandates WHERE account_id=?", (account_id,)).fetchall()

    contacts: list[datetime] = []
    for r in con.execute(
            "SELECT occurred_at, action_type FROM events WHERE account_id=? AND stage=?",
            (account_id, Stage.EXECUTE.value)):
        if r["action_type"] in {a.value for a in (
                ActionType.SEND_MESSAGE, ActionType.SEND_PAYMENT_LINK,
                ActionType.REQUEST_PTP, ActionType.VOICE_CONFIRM_PTP,
                ActionType.SEND_PREDEBIT_NOTICE)}:
            when = datetime.fromisoformat(r["occurred_at"])
            if when <= at:
                contacts.append(when)

    # As of `at`, not as of the end of the batch. Asking what the gate would have said on
    # day 3 must not be answered with a cycle closure that happened on day 14 — otherwise
    # every account in a finished batch denies on POL-STOP-005 and no other rule can be
    # demonstrated.
    terminal = con.execute(
        "SELECT occurred_at, payload FROM events WHERE account_id=? AND stage=?"
        " ORDER BY seq LIMIT 1", (account_id, Stage.CLOSE.value)).fetchone()
    if terminal and datetime.fromisoformat(terminal["occurred_at"]) > at:
        terminal = None
    con.close()

    consent = json.loads(account["consent_json"])
    consent_state = ConsentState(
        channels_allowed=frozenset(Channel(c) for c in consent["channels_allowed"]),
        dnd_registered=consent["dnd_registered"],
        recording_consent=consent["recording_consent"],
        purpose=consent["purpose"],
        # A live batch records no AFA event yet; assume a recent one so the demo's
        # denials come from the rule under test rather than from POL-AFA-002.
        afa_authorised_at=at - timedelta(days=5))

    return GateContext(
        now=at,
        account=Account(
            account_id=account_id,
            merchant_category=MerchantCategory(account["merchant_category"]),
            city_tier=account["city_tier"], consent=consent_state,
            created_at=datetime.fromisoformat(account["created_at"])),
        consent=consent_state,
        mandates=tuple(Mandate(
            mandate_id=m["mandate_id"], account_id=m["account_id"], rail=Rail(m["rail"]),
            cap_paise=m["cap_paise"], status=MandateStatus(m["status"]),
            registered_at=datetime.fromisoformat(m["registered_at"])) for m in mandates),
        cycle=BillingCycle(cycle_id=cycle["cycle_id"], account_id=account_id,
                           amount_paise=cycle["amount_paise"],
                           due_date=datetime.fromisoformat(cycle["due_date"]).date()
                           if "T" in cycle["due_date"]
                           else __import__("datetime").date.fromisoformat(cycle["due_date"]),
                           horizon_days=cycle["horizon_days"]),
        budgets=Budgets(
            attempts_remaining=cfg.budgets.attempts_per_cycle,
            contacts_remaining_week=cfg.budgets.contacts_per_week,
            voice_remaining_cycle=cfg.budgets.voice_per_cycle,
            spend_remaining_paise=cfg.budgets.spend_per_cycle_paise),
        contacts_made=tuple(contacts),
        notices=(),
        ptp=None,
        flags=AccountFlags(
            terminal_state=(json.loads(terminal["payload"])["result"]["terminal_state"]
                            if terminal else None)),
        calendar=Calendar.from_config(cfg),
        cfg=cfg)


def evaluate_hypothetical(path: str | Path, account_id: str, action: Action,
                          at: datetime, cfg: Config,
                          write_to_ledger: bool = True) -> dict[str, Any]:
    """Evaluate without executing. The verdict is written to the ledger with a
    `dry_run` marker, so the denial you produce on stage is itself in the audit trail
    you then show."""
    ctx = context_for(path, account_id, at, cfg)
    policy = PolicySet.from_config(cfg)
    decision = evaluate(action, ctx, policy, decision_id="dec_dryrun")

    if write_to_ledger:
        con = sqlite3.connect(str(path))
        batch_id = con.execute("SELECT batch_id FROM batches LIMIT 1").fetchone()[0]
        con.close()
        led = Ledger(path, batch_id)
        try:
            led.append(EventDraft(
                stage=Stage.GATE, occurred_at=at, account_id=account_id,
                cycle_id=None,          # not attached to the cycle: it changes nothing
                decision_id="dec_dryrun",
                action={k: (v.value if hasattr(v, "value") else v)
                        for k, v in action.__dict__.items() if v is not None},
                action_hash=action.hash(),
                policy={"version": decision.policy_version,
                        "verdict": decision.verdict.value,
                        "checks_passed": list(decision.rule_ids_passed),
                        "check_failed": decision.rule_id_failed,
                        "reason": decision.reason, "basis": decision.basis},
                dry_run=True,
                notes="evaluated via /policy/evaluate; nothing executed"))
        finally:
            led.close()

    return {
        "verdict": decision.verdict.value,
        "rule_id_failed": decision.rule_id_failed,
        "reason": decision.reason,
        "basis": decision.basis,
        "rule_ids_passed": list(decision.rule_ids_passed),
        "policy_version": decision.policy_version,
        "action_hash": decision.action_hash,
        "evaluated_at": decision.evaluated_at.isoformat(),
        "dry_run": True,
    }
