"""The deterministic proposer. This is the control arm, not a degraded mode.

It picks from the eligible set by a fixed priority derived from the cause, using no Q
values and no MDP. That is deliberate and it is a deviation from `07-PROPOSER-GROQ.md`,
which describes the rules proposer as using "the planner's Q values" — doing that would
make it identical to `planner-argmax` and the three-way ablation would have only two
distinct arms.

As built, the ablation measures two separate things:

    rules -> planner-argmax    what the MDP adds over a sensible heuristic
    planner-argmax -> groq     what the LLM adds over the MDP

Both are questions worth an answer.
"""

from __future__ import annotations

from app.domain.enums import ActionType, CauseClass
from app.propose.schema import Proposal

#: What a competent person would try first, per cause, with no arithmetic behind it.
#: The first entry present in the eligible set wins.
PRIORITY: dict[CauseClass, tuple[ActionType, ...]] = {
    # Nothing is wrong their end: get the notice out and try again.
    CauseClass.TRANSIENT_INFRA: (
        ActionType.RETRY_DEBIT, ActionType.SEND_PREDEBIT_NOTICE, ActionType.WAIT),
    # A timing problem. Notice, then debit — the heuristic cannot time it, which is
    # exactly what the MDP is for.
    CauseClass.INSUFFICIENT_FUNDS: (
        ActionType.RETRY_DEBIT, ActionType.SEND_PREDEBIT_NOTICE,
        ActionType.SEND_PAYMENT_LINK, ActionType.REQUEST_PTP, ActionType.WAIT),
    CauseClass.LIMIT_EXCEEDED: (
        ActionType.SPLIT_DEBIT, ActionType.REREGISTER_MANDATE,
        ActionType.SEND_PAYMENT_LINK, ActionType.WAIT),
    CauseClass.AUTH_ARTEFACT: (
        ActionType.SEND_MESSAGE, ActionType.REREGISTER_MANDATE,
        ActionType.SEND_PAYMENT_LINK, ActionType.WAIT),
    # The demo's best moment: repair onto another rail rather than retry a dead mandate.
    CauseClass.MANDATE_INVALID: (
        ActionType.REREGISTER_MANDATE, ActionType.SEND_PAYMENT_LINK, ActionType.WAIT),
    CauseClass.MANDATE_REVOKED: (
        ActionType.SEND_MESSAGE, ActionType.SEND_PAYMENT_LINK, ActionType.CLOSE),
    CauseClass.ACCOUNT_TERMINAL: (
        ActionType.SEND_PAYMENT_LINK, ActionType.ESCALATE_HUMAN, ActionType.CLOSE),
    # You do not know enough yet to spend anything scarce.
    CauseClass.UNKNOWN: (
        ActionType.SEND_PREDEBIT_NOTICE, ActionType.RETRY_DEBIT, ActionType.WAIT),
}


class RulesProposer:
    """No model, no arithmetic, no network. Always available, and the floor everything
    else has to beat."""

    name = "rules"

    def propose(self, state, posterior: dict[CauseClass, float],
                eligible: frozenset[ActionType], **unused) -> Proposal | None:
        """`**unused` absorbs the payload the LLM proposer needs — the amount band, the
        inflow estimate, the mandates. This one reads none of them, and both must present
        the same interface or the ablation is not comparing like with like."""
        cause = max(posterior, key=posterior.get) if posterior else CauseClass.UNKNOWN
        for action in PRIORITY.get(cause, ()):
            if action in eligible:
                return Proposal(
                    action_type=action,
                    rationale=f"first eligible action for {cause.value}",
                    evidence_refs=(f"cause:{cause.value}",
                                   f"posterior:{posterior.get(cause, 0.0):.2f}"),
                    confidence=float(posterior.get(cause, 0.0)),
                    source=self.name)
        # WAIT and CLOSE are always in the eligible set, so this is unreachable in
        # practice; returning None rather than guessing keeps that true by construction.
        return None
