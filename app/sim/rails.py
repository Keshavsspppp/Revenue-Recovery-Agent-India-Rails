"""Rail adapters. `RailAdapter` is the interface a real PSP integration would also
implement — swapping the simulator for a sandbox is a change to this one file.

The gate is enforced *here*, not by convention. A `GateDecision` with `verdict == ALLOW`
whose `action_hash` matches the action being executed is the only way anything reaches a
rail. See docs/05-POLICY-ENGINE.md and CLAUDE.md rule 2.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from app.domain.codemap import NOTICE_WINDOW_VIOLATION, SUCCESS, CodeMap
from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, MandateStatus, Rail
from app.domain.models import Action, AttemptResult, Mandate, NoticeReceipt, make_id
from app.rails.base import (  # noqa: F401  (re-exports; see the note below)
    GateViolation,
    RailAdapter,
    Settlement,
    require_gate,
)

# The Protocol, the gate check and Settlement live in `app/rails/base.py` now, shared
# with the live Razorpay adapter. Re-exported here so existing imports keep working — and
# so the two adapters cannot drift apart on the one thing that matters, which is that
# neither of them will touch a rail without a matching ALLOW.


class SimRailAdapter:
    """Simulated rails. Consults latent balance in exactly one place — step 5 of
    `attempt` — and nowhere else."""

    def __init__(self, cfg: Config, codemap: CodeMap, rng: np.random.Generator,
                 world: Any) -> None:
        self.cfg = cfg
        self.codemap = codemap
        self.rng = rng
        self.world = world          # holds latent state and mandates; see world.py
        self.notices: dict[str, NoticeReceipt] = {}
        self._notice_seq = 0
        self._mandate_seq = 0

    # ---- gate enforcement ----------------------------------------------------

    _require_gate = staticmethod(require_gate)

    # ---- the interface -------------------------------------------------------

    def notify(self, mandate: Mandate, action: Action, at: datetime,
               gate: Any) -> tuple[NoticeReceipt, ...]:
        """Takes the very action the gate signed, exactly as `attempt` does. Rebuilding
        it here would let the two drift and the hash check would be theatre.

        Returns one receipt per presentation the notice covers. A notice for a debit that
        will be split needs a receipt per part, because the amounts have to match what is
        actually presented and POL-NOTICE-003 spends one receipt per attempt.
        """
        self._require_gate(action, gate)
        receipts = []
        for amount_paise in (action.parts or (action.amount_paise or 0,)):
            self._notice_seq += 1
            receipt = NoticeReceipt(
                notice_id=make_id("not", self._notice_seq),
                mandate_id=mandate.mandate_id,
                amount_paise=amount_paise,
                issued_at=at,
                debit_scheduled_for=action.scheduled_for or at,
                merchant_name=self.world.merchant_name(mandate.account_id),
                mandate_reference=mandate.mandate_id,
                opt_out_included=True,
            )
            self.notices[receipt.notice_id] = receipt
            receipts.append(receipt)
        return tuple(receipts)

    def attempt(self, mandate: Mandate, action: Action, at: datetime,
                gate: Any) -> AttemptResult:
        """One presentation of the full amount."""
        self._require_gate(action, gate)
        return self._present(mandate, action.amount_paise or 0, at, action)

    def attempt_split(self, mandate: Mandate, action: Action, at: datetime,
                      gate: Any) -> list[AttemptResult]:
        """Several presentations under one gate decision, one per part.

        The gate signs the split as a whole — the parts are on the action it signed and
        reconcile to the cycle under POL-AMT-001 — so the loop belongs here, beside the
        rail logic, rather than in the executor calling `attempt` with amounts nobody
        approved.

        Each part is a real presentation: its own notice, its own cap check, its own
        return code. Presentation stops at the first structural refusal — a mandate that
        is revoked for part one is revoked for part two, and re-presenting into it would
        manufacture a second decline the issuer never sent.
        """
        self._require_gate(action, gate)
        results: list[AttemptResult] = []
        for part in (action.parts or ()):
            result = self._present(mandate, part, at, action)
            results.append(result)
            if not result.ok and not self._retryable(result.rail_code):
                break
        return results

    def _retryable(self, rail_code: str) -> bool:
        """Whether presenting the next part could plausibly go differently.

        Insufficient funds can: the parts are presented in order and a balance that fell
        short of part one may still be short of part two, but that is the issuer's answer
        to ask for, not ours to assume. A revoked or invalid mandate cannot.
        """
        cause = self.codemap.cause_of(rail_code)
        return cause in (CauseClass.INSUFFICIENT_FUNDS, CauseClass.TRANSIENT_INFRA)

    def _present(self, mandate: Mandate, amount: int, at: datetime,
                 action: Action) -> AttemptResult:
        """Resolution order matters: it decides which code you observe when several
        things are wrong at once. Real rails fail fast on structural checks before they
        ever touch the account."""
        rail = mandate.rail
        latent = self.world.latent[mandate.account_id]
        account = self.world.accounts[mandate.account_id]
        rng = self.world.rng(mandate.account_id)

        # 1. Notice window — rejected at the rail, never reaches the issuer.
        #    Our own defect, counted separately. If this is ever non-zero, we have a bug.
        if not self._notice_satisfied(mandate, amount, at):
            return AttemptResult(False, NOTICE_WINDOW_VIOLATION, None, 0)

        # The debit is now presented to the issuer, so the notice is spent — whatever the
        # issuer says next. A rejected attempt costs a notice exactly like a successful
        # one does; that is what makes attempts scarce. POL-NOTICE-003.
        self._consume_notice(mandate, amount, at, action)

        # 2. Mandate structure
        if mandate.status is MandateStatus.REVOKED:
            return self._fail(rail, CauseClass.MANDATE_REVOKED, rng)
        if mandate.status is MandateStatus.INVALID:
            return self._fail(rail, mandate.defect or CauseClass.MANDATE_INVALID, rng)
        if mandate.status is MandateStatus.PENDING_AFA:
            return self._fail(rail, CauseClass.AUTH_ARTEFACT, rng)

        # 3. Limits — the AFA-free ceiling, then the registered mandate ceiling
        cap = self.cfg.applicable_cap_paise(rail, account.merchant_category)
        if (cap is not None and amount > cap) or amount > mandate.cap_paise:
            return self._fail(rail, CauseClass.LIMIT_EXCEEDED, rng)

        # 4. Infrastructure — time-of-day dependent
        if rng.random() < self.p_infra(at, account.city_tier):
            return self._fail(rail, CauseClass.TRANSIENT_INFRA, rng)

        # 5. Funds — the only check in the system that consults the latent balance
        if latent.balance_paise < amount:
            return self._fail(rail, CauseClass.INSUFFICIENT_FUNDS, rng)

        latent.balance_paise -= amount
        return AttemptResult(True, SUCCESS, at + timedelta(seconds=27),
                             self.cfg.attempt_fee_paise(rail))

    def mandate_status(self, mandate_id: str) -> MandateStatus:
        return self.world.mandates_by_id[mandate_id].status

    def settlement_feed(self, since: datetime) -> list[Settlement]:
        """Settlements confirmed at or after `since`.

        Recovery is counted from this feed, never from an accepted request. On a real
        rail the two are hours apart and a meaningful share of accepted debits never
        settle; reading the feed rather than the API response is what makes the
        distinction structural instead of a promise.
        """
        return [s for s in self.world.settlements if s.settled_at >= since]

    def register_mandate(self, account_id: str, rail: Rail, cap_paise: int,
                         at: datetime, gate: Any) -> Mandate:
        action = Action(type=ActionType.REREGISTER_MANDATE, target_rail=rail)
        self._require_gate(action, gate)
        self._mandate_seq += 1
        mandate = Mandate(
            mandate_id=make_id("mnd", 900_000 + self._mandate_seq),
            account_id=account_id, rail=rail, cap_paise=cap_paise,
            # Registration needs AFA (POL-AFA-002): the mandate is not live until the
            # customer completes it. Whether they do is a world event, not an adapter one.
            status=MandateStatus.PENDING_AFA, registered_at=at)
        self.world.add_mandate(mandate)
        return mandate

    # ---- internals -----------------------------------------------------------

    def p_infra(self, at: datetime, city_tier: int) -> float:
        """Grounded in NPCI's sub-1% technical-decline target and vendor reports of
        degradation during the 19:00-22:00 peak. Declared assumptions, in config."""
        p = self.cfg.sim["p_infra"]
        lo, hi = self.cfg.sim["peak_hours"]
        base = p["base_by_tier"][city_tier]
        mult = p["peak_mult"] if lo <= at.hour < hi else 1.0
        weekend = p["weekend_mult"] if at.weekday() >= 5 else 1.0
        return min(base * mult * weekend, p["cap"])

    def _fail(self, rail: Rail, cause: CauseClass,
              rng: np.random.Generator | None = None) -> AttemptResult:
        return AttemptResult(False, self.reported_code(rail, cause, rng), None, 0)

    def reported_code(self, rail: Rail, cause: CauseClass,
                      rng: np.random.Generator | None = None) -> str:
        """What the bank *says* went wrong, which is not always what did.

        Return codes are the only diagnosis available and they are unreliable: sponsor
        banks populate them inconsistently and the taxonomy itself drifts. This is the
        noise that makes Layer 2 worth having — without it a code map alone would be a
        perfect diagnosis and the posterior would have nothing to add.
        """
        rng = rng if rng is not None else self.rng
        if rng.random() >= self.cfg.sim["code_noise"]:
            return self.codemap.code_for(rail, cause)
        others = sorted(c for c in self.codemap.causes_on(rail) if c is not cause)
        return self.codemap.code_for(rail, others[rng.integers(len(others))])

    def _live_notice(self, mandate: Mandate, amount_paise: int,
                     at: datetime) -> NoticeReceipt | None:
        hours = self.cfg.notice_hours(mandate.rail)
        for receipt in self.notices.values():
            if (receipt.mandate_id == mandate.mandate_id
                    and receipt.amount_paise == amount_paise
                    and receipt.consumed_by_action_hash is None
                    and at - receipt.issued_at >= timedelta(hours=hours)):
                return receipt
        return None

    def _notice_satisfied(self, mandate: Mandate, amount_paise: int, at: datetime) -> bool:
        if self.cfg.notice_hours(mandate.rail) == 0:
            return True
        return self._live_notice(mandate, amount_paise, at) is not None

    def _consume_notice(self, mandate: Mandate, amount_paise: int, at: datetime,
                        action: Action) -> None:
        """POL-NOTICE-003: one notice is consumed by one attempt. A second attempt needs
        a second notice. This is the load-bearing assumption of the whole project —
        see docs/DECISIONS.md."""
        receipt = self._live_notice(mandate, amount_paise, at)
        if receipt is not None:
            self.notices[receipt.notice_id] = NoticeReceipt(
                **{**receipt.__dict__, "consumed_by_action_hash": action.hash()})

    def notice_for(self, mandate: Mandate, amount_paise: int,
                   at: datetime) -> NoticeReceipt | None:
        """The receipt a debit at `at` would consume — the ledger's `notice_ref`."""
        return self._live_notice(mandate, amount_paise, at)


    def current(self, receipt: NoticeReceipt) -> NoticeReceipt:
        """The adapter's own copy of a receipt, which is the one that knows whether it
        has been spent.

        The cycle keeps the receipt it was handed at issue time and never hears about
        consumption, so a gate reading that copy sees every spent notice as live and
        POL-NOTICE-003 cannot fire in a run — the same shape of bug as an opt-out that
        never reaches the consent record. The adapter refusing the presentation is the
        backstop, but a backstop is not enforcement.
        """
        return self.notices.get(receipt.notice_id, receipt)

    def _live_notices(self, mandate: Mandate, amount_paise: int, at: datetime):
        hours = self.cfg.notice_hours(mandate.rail)
        return [r for r in self.notices.values()
                if r.mandate_id == mandate.mandate_id
                and r.amount_paise == amount_paise
                and r.consumed_by_action_hash is None
                and at - r.issued_at >= timedelta(hours=hours)]

    def notices_for(self, mandate, amounts, at):
        """One live receipt per presentation, or None if any is missing.

        Distinct receipts: two equal parts need two notices, and matching the same one
        twice is exactly the double-spend POL-NOTICE-003 exists to forbid. `notice_for`
        would return the same receipt for both.
        """
        matched: list = []
        for amount in amounts:
            receipt = next(
                (r for r in self._live_notices(mandate, amount, at) if r not in matched),
                None)
            if receipt is None:
                return None
            matched.append(receipt)
        return tuple(matched)

