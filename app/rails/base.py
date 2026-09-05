"""The rail interface. One Protocol, two implementations, and the seam between them.

`docs/03-SIMULATOR.md` calls this "the swap point", and the README claims swapping the
simulator for a sandbox is a one-file change behind it. This package is where that claim
gets tested: `simulated.py` is the seeded world the measurement runs on, `razorpay.py`
drives Razorpay's test API with the same agent, the same gate and the same ledger.

The gate is enforced *inside* the adapter, in both implementations. A `GateDecision` with
`verdict == ALLOW` whose `action_hash` matches the action being executed is the only way
anything reaches a rail, real or simulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.enums import MandateStatus, Rail
from app.domain.models import Action, AttemptResult, Mandate, NoticeReceipt


class GateViolation(Exception):
    """Something tried to reach a rail without a matching ALLOW. A bug, not a denial —
    a denial never gets this far."""


@dataclass(frozen=True)
class Settlement:
    account_id: str
    cycle_id: str
    amount_paise: int
    settled_at: datetime
    source: str            # "rail" | "self_pay" | "ptp_kept" | "payment_link"
    reference: str = ""    # the provider's own id, so the trail can be looked up


class RailAdapter(Protocol):
    """What a real integration implements. Both adapters satisfy this exactly."""

    def notify(self, mandate: Mandate, action: Action, at: datetime,
               gate: object) -> tuple[NoticeReceipt, ...]: ...

    def attempt(self, mandate: Mandate, action: Action, at: datetime,
                gate: object) -> AttemptResult: ...

    #: One gate decision, one presentation per part. A split cannot be expressed as
    #: repeated `attempt` calls: the gate signs one action carrying the parts, and an
    #: adapter handed an amount the gate never approved must refuse it.
    def attempt_split(self, mandate: Mandate, action: Action, at: datetime,
                      gate: object) -> list[AttemptResult]: ...

    def mandate_status(self, mandate_id: str) -> MandateStatus: ...

    def register_mandate(self, account_id: str, rail: Rail, cap_paise: int,
                         at: datetime, gate: object) -> Mandate: ...

    def settlement_feed(self, since: datetime) -> list[Settlement]: ...

    #: The live receipts covering a sequence of presentations, or None if any is
    #: missing. Distinct per presentation — matching one receipt twice is the
    #: double-spend POL-NOTICE-003 forbids.
    def notices_for(self, mandate: Mandate, amounts: tuple[int, ...],
                    at: datetime) -> tuple[NoticeReceipt, ...] | None: ...

    #: The adapter's own copy of a receipt — the one that knows if it has been spent.
    def current(self, receipt: NoticeReceipt) -> NoticeReceipt: ...


def require_gate(action: Action, gate: object) -> None:
    """Belt. Ledger invariant 4 is the braces, checked after the run.

    Shared by both adapters deliberately: the enforcement must be identical whether the
    rail is a simulation or a live API, or the simulated result proves nothing about the
    real one.
    """
    if gate is None:
        raise GateViolation(f"{action.type.value} reached a rail with no gate decision")
    if getattr(gate, "verdict", None) != "ALLOW":
        raise GateViolation(
            f"{action.type.value} reached a rail with verdict "
            f"{getattr(gate, 'verdict', None)} ({getattr(gate, 'rule_id_failed', None)})")
    if gate.action_hash != action.hash():
        raise GateViolation(
            f"action_hash mismatch: the gate signed {gate.action_hash}, "
            f"the executor ran {action.hash()}")
