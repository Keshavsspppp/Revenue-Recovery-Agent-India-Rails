"""Promise-to-pay resolution. A pure function over observable facts.

A promise is a commitment with a verifiable outcome, not a CRM note — which means it has
to *resolve*, on a date, against settlement data, without anyone deciding it did. A
broken promise is a stronger signal than a missed payment, because the customer chose
the date themselves.

`PROMISE_ACTIVE` is deliberately not a terminal state (`01-DOMAIN-MODEL.md`): an open
promise pauses the loop, it does not end it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from app.domain.enums import PTPStatus
from app.domain.models import PromiseToPay


def is_open(ptp: PromiseToPay | None, today: date, grace_days: int) -> bool:
    """Whether outreach is suppressed. The window runs to the promised date *plus* grace:
    chasing someone on the morning they said they would pay is how a kept promise becomes
    a complaint."""
    if ptp is None or ptp.status is not PTPStatus.OPEN:
        return False
    return today <= ptp.promised_date + timedelta(days=grace_days)


def due(ptp: PromiseToPay | None, today: date, grace_days: int) -> bool:
    return (ptp is not None and ptp.status is PTPStatus.OPEN
            and today > ptp.promised_date + timedelta(days=grace_days))


def resolve(ptp: PromiseToPay, paid_paise: int, amount_paise: int,
            cycle_ended: bool) -> PTPStatus:
    """What the promise turned out to be worth.

    Resolution is automatic and evidential: it reads what settled, not what anyone
    believed. `LAPSED` is checked first because a cycle that ended before the promised
    date never gave the customer the chance to keep it — recording that as BROKEN would
    punish them for our horizon.
    """
    if paid_paise >= amount_paise:
        return PTPStatus.KEPT
    if cycle_ended:
        return PTPStatus.LAPSED
    if paid_paise > 0:
        return PTPStatus.PARTIAL
    return PTPStatus.BROKEN


def settle_status(ptp: PromiseToPay, status: PTPStatus) -> PromiseToPay:
    return replace(ptp, status=status)


def next_confidence(current: float, status: PTPStatus, decay: float,
                    recovery: float) -> float:
    """Trust in this account's next promise.

    A broken promise is evidence about the *promiser*, so it decays trust multiplicatively
    — repeated breaks compound rather than subtracting linearly. A kept one earns some
    back, but not all of it at once.
    """
    if status is PTPStatus.KEPT:
        return min(1.0, current + recovery * (1.0 - current))
    if status is PTPStatus.BROKEN:
        return max(0.05, current * decay)
    if status is PTPStatus.PARTIAL:
        return max(0.05, current * ((1.0 + decay) / 2.0))
    return current      # LAPSED says nothing about the customer
