"""Drive a small slice of the batch through Razorpay test mode.

The measurement stays where it belongs — 2,000 simulated accounts with a holdout and a
confidence interval, because you cannot randomise a control group against a live provider
and you cannot authorise two thousand mandates in a browser. What this does instead is
prove the seam: the *same* agent, the *same* compliance gate and the *same* ledger,
running against real Razorpay APIs, writing real provider ids into the audit trail.

The demo line is "here is the measured number, and here is the identical agent driving
Razorpay — that payment id is in your dashboard". Both halves, clearly labelled. A demo
that blurs them is worth less than one that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.domain.clock import IST, Clock
from app.domain.config import Config
from app.domain.enums import ActionType, Arm, Rail, Stage, Verdict
from app.domain.models import Action
from app.ledger import EventDraft, Ledger
from app.policy import AccountFlags, Calendar, GateContext, PolicySet, evaluate


class AlreadyClosed(Exception):
    """The batch has finished. A live slice runs on accounts still at risk."""


@dataclass
class LiveResult:
    account_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, str]] = field(default_factory=list)


def run_live_slice(path: str | Path, cfg: Config, adapter, *, accounts: int = 5,
                   dry_run: bool = False) -> list[LiveResult]:
    """Take the first `accounts` at-risk accounts and walk them through the real API.

    Every call still goes through `evaluate()` first. That is the point: if the gate
    refuses a live action it never reaches Razorpay, exactly as it never reaches the
    simulator, and the refusal is in the ledger either way.
    """
    import sqlite3

    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    batch_id = con.execute("SELECT batch_id FROM batches LIMIT 1").fetchone()[0]
    rows = con.execute(
        "SELECT c.account_id, c.cycle_id, c.amount_paise, c.first_failure_code,"
        " c.due_date,"
        " a.merchant_category, a.city_tier FROM cycles c"
        " JOIN accounts a ON a.account_id = c.account_id"
        " ORDER BY c.account_id LIMIT ?", (accounts,)).fetchall()
    con.close()

    con = sqlite3.connect(str(path))
    closed = con.execute("SELECT COUNT(*) FROM events WHERE stage=?",
                         (Stage.CLOSE.value,)).fetchone()[0]
    con.close()
    if closed:
        # The ledger would refuse anyway — nothing may follow a CLOSE — but saying why
        # here is more useful than the invariant firing. A live slice belongs on accounts
        # that are still at risk; a finished batch is a record, not a workload.
        raise AlreadyClosed(
            f"this batch is already complete ({closed} closed cycles), so no further "
            "events may be appended to it. Run the live slice against a freshly "
            "simulated batch:\n"
            "  rr simulate --accounts 200 --seed 7 --out data/live.db\n"
            "  rr live --batch data/live.db --accounts 5")

    led = Ledger(path, batch_id)
    # Already-live accounts are skipped. Every object this creates costs something and is
    # visible to a customer, so a repeated demo run must not quietly duplicate them —
    # the same reasoning as the executor's idempotency keys.
    already = {a for (a,) in led.conn.execute(
        "SELECT DISTINCT account_id FROM events WHERE batch_id=? AND stage=?"
        " AND json_extract(payload,'$.result.provider')='razorpay_test'",
        (batch_id, Stage.EXECUTE.value))}
    assigned = {a for (a,) in led.conn.execute(
        "SELECT account_id FROM events WHERE batch_id=? AND stage=?",
        (batch_id, Stage.ASSIGN.value))}
    policy = PolicySet.from_config(cfg)
    calendar = Calendar.from_config(cfg)
    # Simulated IST, like everywhere else (CLAUDE.md rule 6). The live slice talks to a
    # real API, but the *compliance* clock stays the batch's own: the gate must judge
    # this action by the same clock the audit trail records, and a demo run at 02:00
    # local must not silently become a quiet-hours violation. Mid-morning on the
    # earliest due date is inside every calling window the rules define.
    due = min((date.fromisoformat(str(r["due_date"])[:10]) for r in rows),
              default=date(2026, 9, 1))
    clock = Clock(datetime.combine(due, time(11, 0), tzinfo=IST))
    now = clock.now()
    results: list[LiveResult] = []

    for row in rows:
        result = LiveResult(account_id=row["account_id"])
        if row["account_id"] in already and not dry_run:
            result.steps.append({"action": "SEND_PAYMENT_LINK",
                                 "note": "already live for this account; skipped"})
            results.append(result)
            continue
        # Assignment happens once, first, and before anything else touches the account —
        # the ledger enforces it and refuses a GATE without it. An account being driven
        # live is in treatment by definition, so that is what gets written. It is also
        # why the live slice cannot be folded into the measurement: these accounts were
        # chosen, not randomised.
        if row["account_id"] not in assigned:
            led.append(EventDraft(
                stage=Stage.ASSIGN, occurred_at=now, account_id=row["account_id"],
                cycle_id=row["cycle_id"], arm=Arm.TREATMENT,
                evidence=("assignment:live_slice", "randomised:false"),
                notes="live slice: selected, not randomised — excluded from the measured "
                      "comparison for exactly that reason"))
            assigned.add(row["account_id"])

        ctx = _context(row, cfg, calendar, now)

        # A payment link is the most demonstrable action in the set: it needs no mandate,
        # no notice window and no AFA, and the URL it returns opens in a browser.
        action = Action(type=ActionType.SEND_PAYMENT_LINK, channel=None,
                        template_id="DLT_RECOVERY_LINK_001",
                        cli_series=cfg.raw["policy"]["cli_series_service"])
        # Unique per decision, not per account: a second run — a dry one especially,
        # which is what you do before a demo — used to append a second GATE carrying the
        # first run's id, and two decisions sharing an id is not a decision id.
        decision_id = f"dec_live_{led._seq}_{row['account_id']}"
        decision = evaluate(action, ctx, policy, decision_id=decision_id)
        # Deliberately *not* dry_run. These create real Razorpay objects, and the ledger
        # verifier refuses an EXECUTE marked dry_run precisely so a hypothetical can never
        # be confused with something that reached a provider. It caught this exact mistake
        # on the first live run — the marker is for /policy/evaluate, nothing else.
        led.append(EventDraft(
            stage=Stage.GATE, occurred_at=now, account_id=row["account_id"],
            cycle_id=row["cycle_id"], decision_id=decision_id,
            arm=Arm.TREATMENT, action={"type": action.type.value},
            action_hash=action.hash(),
            policy={"version": decision.policy_version,
                    "verdict": decision.verdict.value,
                    "checks_passed": list(decision.rule_ids_passed),
                    "check_failed": decision.rule_id_failed,
                    "reason": decision.reason, "basis": decision.basis},
            notes="live slice: Razorpay test mode"))

        if decision.verdict is not Verdict.ALLOW:
            result.steps.append({"action": action.type.value, "verdict": "DENY",
                                 "rule": decision.rule_id_failed,
                                 "reason": decision.reason})
            results.append(result)
            continue

        if dry_run:
            result.steps.append({"action": action.type.value, "verdict": "ALLOW",
                                 "note": "dry run, nothing sent"})
            results.append(result)
            continue

        link = adapter.payment_link(row["account_id"], row["amount_paise"], now,
                                    action, decision)
        result.steps.append({"action": action.type.value, "verdict": "ALLOW",
                             "razorpay_id": link["id"], "url": link["url"]})
        result.references.append({"kind": "payment_link", **link})
        led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=now, account_id=row["account_id"],
            cycle_id=row["cycle_id"], decision_id=decision_id,
            arm=Arm.TREATMENT, action={"type": action.type.value},
            action_hash=action.hash(),
            policy={"version": cfg.policy_version},
            result={"provider": "razorpay_test", "provider_id": link["id"],
                    "url": link["url"], "amount_paise": row["amount_paise"]},
            evidence=("provider:razorpay_test", f"provider_id:{link['id']}"),
            notes="live slice: real Razorpay test-mode object"))
        results.append(result)

    led.close()
    return results


def _context(row, cfg: Config, calendar: Calendar, now: datetime) -> GateContext:
    """The same GateContext the simulated runner builds. The gate cannot tell the
    difference, which is the property being demonstrated."""
    from datetime import date

    from app.domain.enums import Channel, MandateStatus, MerchantCategory
    from app.domain.models import Account, BillingCycle, Budgets, ConsentState, Mandate

    consent = ConsentState(channels_allowed=frozenset(Channel),
                           recording_consent=True,
                           afa_authorised_at=now - timedelta(days=5))
    return GateContext(
        now=now,
        account=Account(account_id=row["account_id"],
                        merchant_category=MerchantCategory(row["merchant_category"]),
                        city_tier=row["city_tier"], consent=consent,
                        created_at=now - timedelta(days=300)),
        consent=consent,
        mandates=(Mandate(f"mnd_{row['account_id']}", row["account_id"], Rail.ENACH,
                          max(row["amount_paise"] * 3, 1_500_000),
                          MandateStatus.ACTIVE, now - timedelta(days=200)),),
        cycle=BillingCycle(cycle_id=row["cycle_id"], account_id=row["account_id"],
                           amount_paise=row["amount_paise"],
                           due_date=date(2026, 9, 1)),
        budgets=Budgets(attempts_remaining=cfg.budgets.attempts_per_cycle,
                        contacts_remaining_week=cfg.budgets.contacts_per_week,
                        voice_remaining_cycle=cfg.budgets.voice_per_cycle,
                        spend_remaining_paise=cfg.budgets.spend_per_cycle_paise),
        contacts_made=(), notices=(), ptp=None, flags=AccountFlags(),
        calendar=calendar, cfg=cfg)


def render(results: list[LiveResult], adapter) -> str:
    out: list[str] = []
    add = out.append
    add("LIVE SLICE — Razorpay test mode")
    add("")
    add("  The batch number is simulated. These objects are real: look them up in the")
    add("  Razorpay dashboard. Same agent, same gate, same ledger.")
    add("")
    for r in results:
        add(f"  {r.account_id}")
        for step in r.steps:
            if step.get("verdict") == "DENY":
                add(f"    {step['action']:<20} DENY · {step['rule']} — {step['reason']}")
            elif "razorpay_id" in step:
                add(f"    {step['action']:<20} ALLOW  {step['razorpay_id']}")
                add(f"    {'':<20} {step['url']}")
            else:
                add(f"    {step['action']:<20} {step.get('note', '')}")
    stats = adapter.stats() if hasattr(adapter, "stats") else {}
    if stats:
        add("")
        add(f"  {stats.get('calls', 0)} API calls, {stats.get('errors', 0)} errors, "
            f"{stats.get('created', 0)} objects created "
            f"({', '.join(stats.get('kinds', []))})")
    return "\n".join(out)
