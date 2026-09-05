"""M1 acceptance: the chain detects tampering and the invariants in docs/02-LEDGER.md hold.

These tests are the reason the ledger is built first. Everything downstream trusts them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.domain.clock import IST
from app.domain.enums import ActionType, Arm, Stage, Verdict
from app.ledger import EventDraft, Ledger, LedgerError

T0 = datetime(2026, 9, 3, 9, 0, tzinfo=IST)


def mk(tmp_path, batch_id: str = "bat_test") -> Ledger:
    return Ledger(tmp_path / "batch.db", batch_id)


def detect_and_assign(led: Ledger, account: str, arm: Arm = Arm.TREATMENT,
                      cycle: str = "cyc_1") -> None:
    led.append(EventDraft(stage=Stage.DETECT, occurred_at=T0, account_id=account,
                          cycle_id=cycle))
    led.append(EventDraft(stage=Stage.ASSIGN, occurred_at=T0, account_id=account,
                          cycle_id=cycle, arm=arm))


def gated_execute(led: Ledger, account: str, action: dict, at: datetime,
                  decision_id: str = "dec_1", arm: Arm = Arm.TREATMENT,
                  action_hash: str = "sha256:aa19", **execute_kw) -> None:
    led.append(EventDraft(stage=Stage.GATE, occurred_at=at, account_id=account,
                          cycle_id="cyc_1", decision_id=decision_id, arm=arm,
                          action=action, action_hash=action_hash,
                          policy={"version": "pol_2026.09.1", "verdict": "ALLOW",
                                  "checks_passed": ["POL-QH-001"], "check_failed": None}))
    led.append(EventDraft(stage=Stage.EXECUTE, occurred_at=at, account_id=account,
                          cycle_id="cyc_1", decision_id=decision_id, arm=arm,
                          action=action, action_hash=action_hash,
                          policy={"version": "pol_2026.09.1"}, **execute_kw))


# ---- chain integrity ----------------------------------------------------------

def test_chain_detects_tamper(tmp_path):
    """Mutate one byte of a stored payload; verify() names the exact seq."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.DIAGNOSE, occurred_at=T0, account_id="acc_1",
                          cycle_id="cyc_1", cause_posterior={"INSUFFICIENT_FUNDS": 0.7}))
    assert led.verify().ok

    raw = led.conn.execute("SELECT payload FROM events WHERE seq=2").fetchone()[0]
    tampered = json.loads(raw)
    tampered["cause_posterior"]["INSUFFICIENT_FUNDS"] = 0.99
    led.conn.execute("UPDATE events SET payload=? WHERE seq=2",
                     (json.dumps(tampered, sort_keys=True, separators=(",", ":")),))
    led.conn.commit()

    rep = led.verify()
    assert not rep.ok
    assert rep.first_bad_seq == 2
    assert "hash mismatch" in rep.failures[0]


def test_chain_detects_a_deleted_event(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1"))
    led.conn.execute("DELETE FROM events WHERE seq=1")
    led.conn.commit()
    rep = led.verify()
    assert not rep.ok
    assert any("seq gap" in f for f in rep.failures)


def test_hash_covers_every_field(tmp_path):
    """A change to any payload field must break the chain, not just the indexed ones."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    ev = led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1",
                               notes="original"))
    payload = dict(ev.payload)
    payload["notes"] = "rewritten"
    led.conn.execute("UPDATE events SET payload=? WHERE seq=?",
                     (json.dumps(payload, sort_keys=True, separators=(",", ":")), ev.seq))
    led.conn.commit()
    assert led.verify().first_bad_seq == ev.seq


def test_reopening_continues_the_chain(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    last = led._last_hash
    led.close()

    led2 = Ledger(tmp_path / "batch.db", "bat_test")
    ev = led2.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1"))
    assert ev.prev_hash == last and ev.seq == 2
    assert led2.verify().ok


# ---- write surface ------------------------------------------------------------

def test_no_update_or_delete_on_the_write_surface():
    """CLAUDE.md rule 1: the store exposes append and read queries, nothing else."""
    public = {n for n in dir(Ledger) if not n.startswith("_")}
    assert not {"update", "delete", "edit", "amend"} & public
    assert "append" in public


def test_arm_immutable(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    with pytest.raises(LedgerError, match="already assigned"):
        led.append(EventDraft(stage=Stage.ASSIGN, occurred_at=T0, account_id="acc_1",
                              arm=Arm.HOLDOUT))


def test_nothing_touches_an_account_before_its_arm_is_written(tmp_path):
    led = mk(tmp_path)
    led.append(EventDraft(stage=Stage.DETECT, occurred_at=T0, account_id="acc_1"))
    with pytest.raises(LedgerError, match="before ASSIGN"):
        led.append(EventDraft(stage=Stage.DIAGNOSE, occurred_at=T0, account_id="acc_1"))


def test_no_events_after_close(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.CLOSE, occurred_at=T0, account_id="acc_1",
                          cycle_id="cyc_1", result={"terminal_state": "RECOVERED"}))
    with pytest.raises(LedgerError, match="closed"):
        led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1",
                              cycle_id="cyc_1"))


def test_corrections_are_appended_not_edited(tmp_path):
    """The only way to fix a wrong event is a new event pointing at it."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    wrong = led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0,
                                  account_id="acc_1", notes="mis-scored"))
    led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1",
                          corrects=wrong.event_id, notes="corrected"))
    assert led.verify().ok
    assert len(led.timeline("acc_1")) == 4


# ---- invariants ---------------------------------------------------------------

def test_no_execute_without_allow(tmp_path):
    """Invariant 4 — the machine-checkable form of 'nothing reached a rail ungated'."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.EXECUTE, occurred_at=T0, account_id="acc_1",
                          decision_id="dec_1", action={"type": "SEND_MESSAGE"},
                          action_hash="sha256:beef",
                          policy={"version": "pol_2026.09.1"}))
    rep = led.verify()
    assert not rep.ok and "no matching ALLOW gate" in rep.failures[0]


def test_execute_with_a_denying_gate_fails(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.GATE, occurred_at=T0, account_id="acc_1",
                          decision_id="dec_1", action={"type": "VOICE_CONFIRM_PTP"},
                          action_hash="sha256:beef",
                          policy={"version": "pol_2026.09.1",
                                  "verdict": Verdict.DENY.value,
                                  "check_failed": "POL-QH-001"}))
    led.append(EventDraft(stage=Stage.EXECUTE, occurred_at=T0, account_id="acc_1",
                          decision_id="dec_1", action={"type": "VOICE_CONFIRM_PTP"},
                          action_hash="sha256:beef", policy={"version": "pol_2026.09.1"}))
    assert not led.verify().ok


def test_execute_with_a_mismatched_action_hash_fails(tmp_path):
    """A gate signs one action. Executing a different one is not covered by it."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.GATE, occurred_at=T0, account_id="acc_1",
                          decision_id="dec_1", action={"type": "SEND_MESSAGE"},
                          action_hash="sha256:allowed",
                          policy={"verdict": "ALLOW", "version": "pol_2026.09.1"}))
    led.append(EventDraft(stage=Stage.EXECUTE, occurred_at=T0, account_id="acc_1",
                          decision_id="dec_1", action={"type": "SEND_MESSAGE"},
                          action_hash="sha256:swapped",
                          policy={"version": "pol_2026.09.1"}))
    assert not led.verify().ok


def test_holdout_never_treated(tmp_path):
    """Invariant 5 — the holdout gets the merchant default and nothing else."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_h", arm=Arm.HOLDOUT)
    gated_execute(led, "acc_h", {"type": ActionType.RETRY_DEBIT.value}, T0,
                  arm=Arm.HOLDOUT, notice_ref="not_1",
                  result={"ok": False, "rail_code": "AP01", "settled_at": None})
    # a notice at T0 - 25h, so the debit above satisfies invariant 8
    assert "contaminated" not in " ".join(led.verify().failures)

    led2 = mk(tmp_path / "second", "bat_2")
    detect_and_assign(led2, "acc_h", arm=Arm.HOLDOUT)
    gated_execute(led2, "acc_h", {"type": ActionType.VOICE_CONFIRM_PTP.value}, T0,
                  arm=Arm.HOLDOUT)
    assert any("contaminated" in f for f in led2.verify().failures)


def test_close_requires_a_terminal_state(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.CLOSE, occurred_at=T0, account_id="acc_1",
                          cycle_id="cyc_1", result={"reason": "done"}))
    assert any("terminal_state" in f for f in led.verify().failures)


def test_settlement_truth_not_acknowledgement(tmp_path):
    """Invariant 7 — an ok result with no settled_at is an API 200, not recovered money."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1",
                          result={"ok": True, "rail_code": "SUCCESS", "settled_at": None}))
    assert any("settled_at" in f for f in led.verify().failures)


def test_notice_precedes_retry(tmp_path):
    """Invariant 8 — POL-NOTICE-001 checked after the fact, from the raw events."""
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    notice_at = T0
    gated_execute(led, "acc_1", {"type": ActionType.SEND_PREDEBIT_NOTICE.value},
                  notice_at, decision_id="dec_n", action_hash="sha256:notice",
                  result={"notice_id": "not_1"})

    too_soon = notice_at + timedelta(hours=23)
    gated_execute(led, "acc_1", {"type": ActionType.RETRY_DEBIT.value}, too_soon,
                  decision_id="dec_r", action_hash="sha256:retry", notice_ref="not_1",
                  result={"ok": False, "rail_code": "AP01", "settled_at": None})
    rep = led.verify()
    assert not rep.ok and any("inside the 24h window" in f for f in rep.failures)


def test_notice_at_exactly_24h_is_allowed(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    gated_execute(led, "acc_1", {"type": ActionType.SEND_PREDEBIT_NOTICE.value}, T0,
                  decision_id="dec_n", action_hash="sha256:notice",
                  result={"notice_id": "not_1"})
    gated_execute(led, "acc_1", {"type": ActionType.RETRY_DEBIT.value},
                  T0 + timedelta(hours=24), decision_id="dec_r",
                  action_hash="sha256:retry", notice_ref="not_1",
                  result={"ok": False, "rail_code": "AP01", "settled_at": None})
    assert led.verify().ok, led.verify().failures


def test_retry_with_no_notice_at_all_fails(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    gated_execute(led, "acc_1", {"type": ActionType.RETRY_DEBIT.value}, T0,
                  result={"ok": False, "rail_code": "AP01", "settled_at": None})
    assert any("no referenced pre-debit notice" in f for f in led.verify().failures)


# ---- read queries the demo needs ----------------------------------------------

def test_read_queries(tmp_path):
    led = mk(tmp_path)
    detect_and_assign(led, "acc_1")
    led.append(EventDraft(stage=Stage.GATE, occurred_at=T0, account_id="acc_1",
                          decision_id="dec_9", action={"type": "VOICE_CONFIRM_PTP"},
                          policy={"verdict": "DENY", "check_failed": "POL-QH-001"}))
    led.append(EventDraft(stage=Stage.OBSERVE, occurred_at=T0, account_id="acc_1",
                          arm=Arm.TREATMENT,
                          result={"ok": True, "rail_code": "SUCCESS",
                                  "settled_at": T0.isoformat(), "amount_paise": 149900}))
    assert [e.stage for e in led.timeline("acc_1")][:2] == ["DETECT", "ASSIGN"]
    assert len(led.decision("dec_9")) == 1
    assert led.denials_by_rule() == [("POL-QH-001", 1)]
    assert led.recovery_by_arm() == [("treatment", 1, 149900)]


def test_determinism_across_runs(tmp_path):
    """CLAUDE.md rule 5: same inputs, byte-identical ledger except wall_clock_at."""
    def build(path) -> list[dict]:
        led = Ledger(path, "bat_det")
        detect_and_assign(led, "acc_1")
        led.append(EventDraft(stage=Stage.DIAGNOSE, occurred_at=T0, account_id="acc_1",
                              cause_posterior={"INSUFFICIENT_FUNDS": 0.71}))
        out = [dict(e.payload) for e in led.all_events()]
        led.close()
        return out

    a = build(tmp_path / "a.db")
    b = build(tmp_path / "b.db")
    for x, y in zip(a, b):
        x.pop("wall_clock_at"), y.pop("wall_clock_at")
        x.pop("hash"), y.pop("hash")          # hash covers wall_clock_at
        x.pop("prev_hash"), y.pop("prev_hash")
        assert x == y


def test_verify_is_clean_on_a_well_formed_batch(tmp_path):
    led = mk(tmp_path)
    for i in range(3):
        acc = f"acc_{i}"
        arm = Arm.HOLDOUT if i == 0 else Arm.TREATMENT
        led.append(EventDraft(stage=Stage.DETECT, occurred_at=T0, account_id=acc,
                              cycle_id=f"cyc_{i}"))
        led.append(EventDraft(stage=Stage.ASSIGN, occurred_at=T0, account_id=acc,
                              cycle_id=f"cyc_{i}", arm=arm))
        led.append(EventDraft(stage=Stage.CLOSE, occurred_at=T0 + timedelta(days=30),
                              account_id=acc, cycle_id=f"cyc_{i}", arm=arm,
                              result={"terminal_state": "CYCLE_ENDED"}))
    rep = led.verify()
    assert rep.ok and rep.events == 9 and rep.first_bad_seq is None
