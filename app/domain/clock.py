"""The only module allowed to read real time.

Compliance rules are evaluated against the *simulated* clock in IST. `wall_clock()`
exists solely for the ledger's `wall_clock_at` field, which is the one field permitted
to differ between two runs with the same seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def wall_clock() -> datetime:
    """Real UTC. Ledger provenance only — never compliance logic, never control flow."""
    return datetime.now(timezone.utc)


@dataclass
class Clock:
    """Simulated IST clock. One per run, passed explicitly."""

    _now: datetime

    def __post_init__(self) -> None:
        if self._now.tzinfo is None:
            raise ValueError("Clock requires a timezone-aware datetime")
        self._now = self._now.astimezone(IST)

    @classmethod
    def at(cls, iso: str) -> Clock:
        return cls(datetime.fromisoformat(iso))

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def advance(self, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        self._now += timedelta(days=days, hours=hours, minutes=minutes)
        return self._now

    def advance_to(self, when: datetime | date, at_time: time | None = None) -> datetime:
        """Move forward to `when`. Never moves backwards — that would corrupt the ledger."""
        if isinstance(when, datetime):
            target = when.astimezone(IST)
        else:
            target = datetime.combine(when, at_time or time(9, 0), tzinfo=IST)
        if target < self._now:
            raise ValueError(f"clock cannot go backwards: {self._now} -> {target}")
        self._now = target
        return self._now
