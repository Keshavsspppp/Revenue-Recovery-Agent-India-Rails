"""Choosing one action: posterior-weighted argmax over per-cause Q values.

`06-PLANNER.md` offers two ways to handle an uncertain cause. This takes option 1 — solve
the MDP per cause class, then weight — because it keeps `Q` inspectable per cause, which
is what the audit trail has to show. A belief-state MDP would be more correct under high
posterior entropy and much harder to explain; the note in docs/DECISIONS.md says so.

The stopping rule lives here rather than inside the MDP. `CLOSE` is worth exactly zero and
`WAIT` costs nothing, so inside the solver they tie whenever nothing else is worth doing.
Stating it explicitly — *no substantive action has positive expected value, so stop* — is
both the doc's own phrasing and the thing you can read aloud from the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, TerminalState
from app.domain.money import format_inr
from app.plan.mdp import ACTIONS, PlanState, q_at, solution_for

#: Neither of these is an intervention: one spends nothing and one gives up. The stopping
#: rule asks whether anything *substantive* is worth doing.
NON_SUBSTANTIVE = frozenset({ActionType.WAIT, ActionType.CLOSE})


@dataclass(frozen=True)
class Choice:
    action: ActionType
    q_values: dict[ActionType, float]
    terminal: TerminalState | None
    reason: str
    weighted_value: float

    @property
    def is_close_call(self) -> float | None:
        """How near the two best substantive actions are, as a fraction of the better.

        `None` when there is nothing to choose between — fewer than two substantive
        actions, or the leader is worthless anyway.
        """
        substantive = sorted((q for a, q in self.q_values.items()
                              if a not in NON_SUBSTANTIVE and q > -1e17), reverse=True)
        if len(substantive) < 2 or substantive[0] <= 0:
            return None
        return (substantive[0] - substantive[1]) / abs(substantive[0])

    def evidence(self) -> tuple[str, ...]:
        """Ledger evidence: a number per action, reconstructable by hand."""
        return tuple(f"q:{a.value}:{int(q)}" for a, q in sorted(
            self.q_values.items(), key=lambda kv: -kv[1]) if q > -1e17)


def choose(state: PlanState, posterior: dict[CauseClass, float],
           eligible: frozenset[ActionType], amount_paise: int, concentration: float,
           cfg: Config, amount_scale: int,
           lambda_harm: float | None = None, overlay: str | None = None,
           split_parts_n: int = 2) -> Choice:
    """a* = argmax over eligible a of the sum over causes of P(c) . Q_c(s, a).

    Except under the hardship overlay, which is not an expected-value question at all.
    RBI's draft norms expect a lender to identify borrowers in repayment difficulty and
    offer guidance; that is a rule about conduct, and pricing it would be asking whether
    it is *profitable* to stop chasing someone who cannot pay. It is not meant to be.
    """
    if overlay == "hardship":
        return Choice(
            ActionType.OFFER_ACCOMMODATION, {}, TerminalState.HARDSHIP,
            "hardship signals above threshold: routed out of recovery into an "
            "accommodation offer, by rule rather than by expected value", 0.0)
    weighted: dict[ActionType, float] = {a: 0.0 for a in ACTIONS}
    reachable: set[ActionType] = set()
    weighted_value = 0.0

    for cause, mass in posterior.items():
        if mass <= 0.0:
            continue
        solution = solution_for(cause, amount_paise, concentration, cfg,
                                lambda_harm=lambda_harm, amount_scale=amount_scale,
                                split_parts_n=split_parts_n)
        # V(s) is what this state is worth under this cause *including everything the
        # agent could still do later*. Weighting it by the posterior is how the stopping
        # rule prices the chance that the diagnosis is wrong.
        weighted_value += mass * solution.value(state)
        for action, q in q_at(solution, state, cfg).items():
            if q > -1e17:
                weighted[action] += mass * q
                reachable.add(action)

    # The eligible set is the gate between diagnosis and proposal; the planner may only
    # choose from what it produced.
    available = {a: weighted[a] for a in ACTIONS
                 if a in eligible and a in reachable}
    if not available:
        return Choice(ActionType.CLOSE, weighted, TerminalState.EV_BELOW_THRESHOLD,
                      "no action in the eligible set is reachable from this state", 0.0)

    # Stop on the value of the *state*, not on the value of acting today.
    #
    # Closing is irreversible and waiting is free, so an agent that closes whenever no
    # action is worth taking *right now* throws away everything it could have done later
    # — including on the mass of the posterior where its diagnosis is wrong. V(s) already
    # accounts for both: it is the value of this state under optimal play to the horizon,
    # and weighting it by the posterior prices the chance the cause is misread.
    #
    # This matters because the codes are noisy. An account reported MANDATE_REVOKED that
    # is really short of funds has a live mandate and a real chance of collecting; under
    # the old rule the eligible set vetoed every debit, the best action today was nothing,
    # and the agent closed a recoverable account for good.
    if weighted_value <= float(cfg.planner["close_threshold_paise"]):
        substantive = {a: q for a, q in available.items() if a not in NON_SUBSTANTIVE}
        best_name = (max(substantive, key=substantive.get).value if substantive
                     else "none")
        return Choice(
            ActionType.CLOSE, weighted, TerminalState.EV_BELOW_THRESHOLD,
            f"nothing now or later is worth doing: this state is worth "
            f"{format_inr(int(weighted_value))} under the full posterior "
            f"(best action {best_name})",
            weighted_value)

    action = max(available, key=available.get)
    return Choice(action, weighted, None,
                  f"highest expected value of {len(available)} eligible actions",
                  available[action])
