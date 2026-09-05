"""The cause -> eligible action matrix. docs/04-CAUSE-TAXONOMY.md.

This is the gate between diagnosis and proposal. The planner picks among what this
returns; the LLM may only choose from what this returns. Nothing outside it is
schedulable.

The matrix has two columns, and the second one matters as much as the first. Encoding only
"what you may do" would let a 20%-probable structural defect be papered over by an action
that is eligible for the 80% cause — so the prohibitions are encoded too, and they win.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, MandateStatus, Rail
from app.domain.models import Mandate
from app.domain.money import split_parts

#: What each cause permits. Read with WRONG below, which subtracts from it.
ALLOWED: dict[CauseClass, frozenset[ActionType]] = {
    CauseClass.TRANSIENT_INFRA: frozenset({
        ActionType.RETRY_DEBIT, ActionType.SEND_PREDEBIT_NOTICE}),
    CauseClass.INSUFFICIENT_FUNDS: frozenset({
        ActionType.SEND_PREDEBIT_NOTICE, ActionType.RETRY_DEBIT,
        ActionType.REQUEST_PTP, ActionType.SEND_PAYMENT_LINK}),
    CauseClass.LIMIT_EXCEEDED: frozenset({
        ActionType.SPLIT_DEBIT, ActionType.REREGISTER_MANDATE,
        ActionType.SEND_PAYMENT_LINK}),
    CauseClass.AUTH_ARTEFACT: frozenset({
        ActionType.SEND_MESSAGE, ActionType.REREGISTER_MANDATE,
        ActionType.SEND_PAYMENT_LINK}),
    CauseClass.MANDATE_INVALID: frozenset({
        ActionType.REREGISTER_MANDATE, ActionType.SEND_PAYMENT_LINK}),
    CauseClass.MANDATE_REVOKED: frozenset({
        ActionType.SEND_MESSAGE, ActionType.SEND_PAYMENT_LINK}),
    CauseClass.ACCOUNT_TERMINAL: frozenset({
        ActionType.SEND_PAYMENT_LINK, ActionType.ESCALATE_HUMAN}),
    CauseClass.UNKNOWN: frozenset({
        ActionType.SEND_PREDEBIT_NOTICE, ActionType.RETRY_DEBIT}),
}

# The "explicitly wrong" column of the matrix splits in two, and the split is the whole
# point of this module's handling of an uncertain diagnosis.
#
#   WRONG_HARM   doing this to a *customer* would be inappropriate. Pestering someone
#                whose bank timed out, asking for work that cannot help, escalating on a
#                diagnosis you do not have. Vetoed if *any* plausible cause forbids it —
#                the conservative direction, because the cost of being wrong lands on a
#                person rather than on a budget.
#
#   WRONG_FUTILE this would simply not work. Retrying a dead mandate, retrying the same
#                amount over the same cap. Vetoed only when the causes forbidding it hold
#                the *majority* of the posterior.
#
# The second is the fix for a real defect. Treating a 25%-probable structural claim as an
# absolute veto is not conservatism, it is miscalibration: a wasted retry costs Rs 2.50
# and the planner already prices that exactly. Refusing it forfeits a 75% chance at the
# whole cycle. With 12% code noise the codes make that claim wrongly all the time, and
# under the old rule every one of those accounts was closed for good.
WRONG_HARM: dict[CauseClass, frozenset[ActionType]] = {
    CauseClass.TRANSIENT_INFRA: frozenset({
        ActionType.SEND_MESSAGE, ActionType.REQUEST_PTP,
        ActionType.VOICE_CONFIRM_PTP, ActionType.REREGISTER_MANDATE}),
    CauseClass.INSUFFICIENT_FUNDS: frozenset({
        ActionType.SEND_MESSAGE, ActionType.REREGISTER_MANDATE}),
    CauseClass.LIMIT_EXCEEDED: frozenset(),
    CauseClass.AUTH_ARTEFACT: frozenset(),
    CauseClass.MANDATE_INVALID: frozenset(),
    # Re-registering without fresh consent is a compliance question, not an efficacy one.
    # POL-AFA-002 refuses it at the gate as well; two guards, deliberately.
    CauseClass.MANDATE_REVOKED: frozenset({ActionType.REREGISTER_MANDATE}),
    CauseClass.ACCOUNT_TERMINAL: frozenset({
        ActionType.REREGISTER_MANDATE, ActionType.REQUEST_PTP,
        ActionType.VOICE_CONFIRM_PTP}),
    CauseClass.UNKNOWN: frozenset({
        ActionType.ESCALATE_HUMAN, ActionType.VOICE_CONFIRM_PTP}),
}

WRONG_FUTILE: dict[CauseClass, frozenset[ActionType]] = {
    CauseClass.TRANSIENT_INFRA: frozenset(),
    CauseClass.INSUFFICIENT_FUNDS: frozenset(),
    # Retrying the same amount on the same mandate cannot clear a cap.
    CauseClass.LIMIT_EXCEEDED: frozenset({ActionType.RETRY_DEBIT}),
    # A silent retry changes nothing: the customer has to act.
    CauseClass.AUTH_ARTEFACT: frozenset({
        ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT}),
    # No number of attempts fixes an NRE account.
    CauseClass.MANDATE_INVALID: frozenset({
        ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT}),
    CauseClass.MANDATE_REVOKED: frozenset({
        ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT}),
    CauseClass.ACCOUNT_TERMINAL: frozenset({
        ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT}),
    CauseClass.UNKNOWN: frozenset(),
}

#: The union of both, kept for callers that want the whole "explicitly wrong" column.
WRONG: dict[CauseClass, frozenset[ActionType]] = {
    c: WRONG_HARM[c] | WRONG_FUTILE[c] for c in CauseClass}

#: Always available, under every cause. `WAIT` because an agent that cannot choose to do
#: nothing is firing rather than optimising, and `CLOSE` because the stopping rule is an
#: economic result rather than a constant — the planner reaches for it when the best
#: remaining action prices out negative.
UNIVERSAL: frozenset[ActionType] = frozenset({ActionType.WAIT, ActionType.CLOSE})

#: The hardship overlay. Knowing who *not* to chase is the most differentiated thing here.
HARDSHIP_SET: frozenset[ActionType] = frozenset({
    ActionType.OFFER_ACCOMMODATION, ActionType.ESCALATE_HUMAN, ActionType.CLOSE})


@dataclass(frozen=True)
class EligibleSet:
    """What may be scheduled, and the rails it may touch."""

    actions: frozenset[ActionType]
    forbidden_rails: frozenset[Rail] = frozenset()   # a debit must not go here
    target_rails: frozenset[Rail] = frozenset()      # a re-registration could go here
    overlay: str | None = None                       # "hardship" | "ptp_open" | None
    plausible_causes: tuple[CauseClass, ...] = ()

    def __contains__(self, action_type: object) -> bool:
        return action_type in self.actions

    def __iter__(self):
        return iter(sorted(self.actions, key=lambda a: a.value))

    def __len__(self) -> int:
        return len(self.actions)


def plausible(posterior: dict[CauseClass, float], threshold: float) -> tuple[CauseClass, ...]:
    """Causes carrying at least `threshold` of the mass, plus the argmax so the set is
    never empty when the posterior is flat."""
    above = tuple(c for c, p in sorted(posterior.items(), key=lambda kv: -kv[1])
                  if p >= threshold)
    if above:
        return above
    return (max(posterior, key=posterior.get),) if posterior else (CauseClass.UNKNOWN,)


def eligible_actions(posterior: dict[CauseClass, float], mandates: tuple[Mandate, ...],
                     cfg: Config, *, hardship_score: float = 0.0,
                     ptp_open: bool = False, amount_paise: int | None = None,
                     category=None, afa_fresh: bool = True) -> EligibleSet:
    """The eligible set for a state.

    Overlays are applied *after* the matrix and they replace it, because both of them
    describe situations where the cause has stopped being the relevant question.
    """
    causes = plausible(posterior, float(cfg.raw["diagnose"]["plausible_threshold"]))

    if ptp_open:
        # An open promise pauses the loop; it does not end it.
        return EligibleSet(actions=frozenset({ActionType.WAIT}), overlay="ptp_open",
                           plausible_causes=causes)

    if hardship_score > cfg.policy.hardship_threshold:
        # RBI's draft norms expect lenders to identify borrowers in difficulty and offer
        # guidance. An agent that knows who not to chase is worth more than one more
        # attempt.
        return EligibleSet(actions=HARDSHIP_SET, overlay="hardship",
                           plausible_causes=causes)

    allowed: set[ActionType] = set()
    forbidden: set[ActionType] = set()
    for cause in causes:
        allowed |= ALLOWED.get(cause, frozenset())
        # A harm prohibition under any plausible cause is absolute.
        forbidden |= WRONG_HARM.get(cause, frozenset())

    # A futility prohibition needs the majority of the belief behind it. Below that the
    # action stays on the table and the planner prices it against the mass supporting it,
    # which is what it is for.
    threshold = float(cfg.raw["diagnose"]["futile_veto_threshold"])
    futile_mass: dict[ActionType, float] = {}
    for cause, mass in posterior.items():
        for action in WRONG_FUTILE.get(cause, frozenset()):
            futile_mass[action] = futile_mass.get(action, 0.0) + mass
    forbidden |= {a for a, mass in futile_mass.items() if mass >= threshold}

    actions = (allowed - forbidden) | UNIVERSAL

    broken = frozenset(m.rail for m in mandates
                       if m.status in (MandateStatus.INVALID, MandateStatus.REVOKED,
                                       MandateStatus.PENDING_AFA))
    healthy = frozenset(m.rail for m in mandates if m.status is MandateStatus.ACTIVE)
    # A rail worth moving to: one this account does not already hold a broken mandate on.
    targets = frozenset(r for r in Rail if r is not Rail.PAYMENT_LINK) - broken - healthy

    if ActionType.REREGISTER_MANDATE in actions and not targets:
        # Nowhere to repair it to. Offering the action anyway would let the planner price
        # a move that cannot happen.
        actions = actions - {ActionType.REREGISTER_MANDATE}
    if any(m.status is MandateStatus.PENDING_AFA for m in mandates):
        # An authorisation is already with the customer. Asking them to authorise a second
        # mandate on another rail while the first is outstanding is asking twice for the
        # same thing — the account timeline showed the agent doing exactly that on
        # consecutive days before this.
        actions = actions - {ActionType.REREGISTER_MANDATE}
    if not healthy:
        actions = actions - {ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT,
                             ActionType.SEND_PREDEBIT_NOTICE}


    # An action the gate will certainly refuse is not eligible. Leaving it in would let
    # the planner price something that can never happen, then watch it be denied — which
    # wastes the decision and puts a phantom in the audit trail. These two mirror
    # POL-AFA-001 and POL-AFA-002 exactly.
    parts: tuple[int, ...] = ()
    # The *same* mandate the executor will debit: the first active one, in the order the
    # account holds them. Picking the lowest-sorted rail here instead meant the ceiling
    # was computed against one mandate and the debit presented on another — the notice
    # was then issued for the wrong shape and the gate refused the retry it had just
    # declared eligible. Seventeen POL-NOTICE-001 denials on one 600-account batch, all
    # of them the two definitions disagreeing.
    primary = next((m for m in mandates if m.status is MandateStatus.ACTIVE), None)
    if amount_paise is not None and primary is not None and category is not None:
        cap = cfg.applicable_cap_paise(primary.rail, category)
        effective = min([c for c in (cap, primary.cap_paise) if c is not None],
                        default=None)
        parts = split_parts(amount_paise, effective,
                            int(cfg.planner["split_max_parts"]))
        if effective is not None and amount_paise > effective:
            # Over the ceiling, a retry is not a debit that can happen — so the split
            # *replaces* it wherever it was offered, rather than being a separate action
            # the cause matrix has to have anticipated.
            #
            # The ceiling is structural: it binds whatever the money failed for. Reading
            # the split off the cause alone left 74 of 600 accounts in one batch over the
            # cap with no debit at all — retry stripped as futile, split never offered
            # because the code said INSUFFICIENT_FUNDS rather than LIMIT_EXCEEDED — so
            # the agent issued a notice it could never act on and then waited out the
            # horizon.
            if ActionType.RETRY_DEBIT in actions and len(parts) >= 2:
                actions = (actions - {ActionType.RETRY_DEBIT}) | {ActionType.SPLIT_DEBIT}
            else:
                actions = actions - {ActionType.RETRY_DEBIT}
            if len(parts) < 2:
                # Over the ceiling and no permitted split reaches it. There is no debit
                # to notify about, so the notice goes too — and the planner is left with
                # the actions that can actually collect this amount: a payment link, a
                # promise, a repaired mandate on a rail with a higher ceiling.
                actions = actions - {ActionType.SEND_PREDEBIT_NOTICE}
        # A split of one part is a retry with extra steps: same presentation, same
        # ceiling, and the planner would be choosing between an action and itself. Only
        # stripped when the cap was actually evaluated — a caller that supplies no
        # amount or category has told us nothing about the ceiling, and guessing "no
        # split needed" there would silently remove the remedy for LIMIT_EXCEEDED.
        if len(parts) < 2:
            actions = actions - {ActionType.SPLIT_DEBIT}
    if not afa_fresh:
        actions = actions - {ActionType.REREGISTER_MANDATE}

    return EligibleSet(actions=frozenset(actions), forbidden_rails=broken,
                       target_rails=targets, overlay=None, plausible_causes=causes)


def explain(eligible: EligibleSet) -> list[str]:
    """Evidence strings for the ELIGIBLE ledger event. Each one reconstructable — a code,
    a count, a name — never a sentence."""
    out = [f"plausible_cause:{c.value}" for c in eligible.plausible_causes]
    out += [f"eligible:{a.value}" for a in eligible]
    out += [f"forbidden_rail:{r.value}" for r in sorted(eligible.forbidden_rails,
                                                        key=lambda r: r.value)]
    if eligible.overlay:
        out.append(f"overlay:{eligible.overlay}")
    return out
