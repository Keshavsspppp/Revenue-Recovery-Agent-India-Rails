"""The oracle: the same problem, solved with the customer's balance in hand.

This is a **diagnostic, never a reported result.** It lives under `app/sim/` precisely
because it reads latent state — putting it anywhere else would either break the module
boundary or, worse, quietly not break it.

What it is for: `agent / oracle` is the share of achievable value captured. If the agent
lands close to the oracle, the inflow estimator is good and the remaining gap is
irreducible; if it lands far below, the fix is better estimation rather than more actions.
Reporting that ratio tells a room how much headroom is left, which is a stronger and much
less common claim than a recovery rate on its own.

The oracle still obeys every constraint the agent obeys — the notice window, the attempt
budget, the compliance gate. It is clairvoyant, not exempt.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.domain.clock import IST


def oracle_policy(ex, world, account_id: str, day: date, offset: int) -> None:
    """Spend attempts only on days the money is actually there.

    The agent has to infer the inflow phase from the timestamps of past successful
    debits. The oracle just looks. Everything else about the two is the same, so the gap
    between them is exactly the value of knowing.
    """
    mandate = world.primary_mandate(account_id)
    if mandate is None:
        return
    state = world.cycles[account_id]
    latent = world.latent[account_id]
    amount = state.cycle.amount_paise
    attempts_left = ex.cfg.budgets.attempts_per_cycle - max(0, len(state.attempts) - 1)
    if attempts_left <= 0:
        return

    funded = latent.balance_paise >= amount
    live_notice = world.rails.notice_for(mandate, amount, _at(day, ex.attempt_time))

    if live_notice is not None and funded:
        ex.retry(account_id, mandate, _at(day, ex.attempt_time))
        return

    # Commit a day in advance, which is the constraint that defines the whole problem:
    # buy the option now, either because the money is already there or because tomorrow
    # is the day it arrives.
    tomorrow = day + timedelta(days=1)
    if live_notice is None and (funded or tomorrow.day == latent.inflow_day):
        ex.notice(account_id, mandate, _at(day, ex.notice_time),
                  _at(tomorrow, ex.attempt_time))


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=IST)
