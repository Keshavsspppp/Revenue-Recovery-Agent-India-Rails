"""Money is integer paise. No floats, anywhere. Format only at the edge."""

from __future__ import annotations


class Money(int):
    """Paise. Use Money.rupees(1499.00) at the edge only."""

    __slots__ = ()

    @classmethod
    def rupees(cls, amount: float) -> Money:
        return cls(round(amount * 100))


def format_inr(paise: int) -> str:
    """Indian digit grouping: 57820000 -> '₹5,78,200.00'. Display only."""
    sign = "-" if paise < 0 else ""
    rupees, sub = divmod(abs(int(paise)), 100)
    head, tail = str(rupees)[:-3], str(rupees)[-3:]
    if head:
        # group the part above the last three digits in pairs, right to left
        pairs = [head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1]
        tail = ",".join(pairs) + "," + tail
    return f"{sign}₹{tail}.{sub:02d}"


#: Stratification and prompt band. The proposer sees the band, never the amount — it
#: does not need the rupee figure to pick an action, and withholding it makes emitting
#: one structurally impossible (docs/07-PROPOSER-GROQ.md).
AMOUNT_BANDS: tuple[tuple[int, str], ...] = (
    (500, "0-500"), (1000, "500-1000"), (2500, "1000-2500"), (5000, "2500-5000"),
    (15000, "5000-15000"), (50000, "15000-50000"),
)


def amount_band(paise: int) -> str:
    rupees = paise / 100
    for hi, label in AMOUNT_BANDS:
        if rupees < hi:
            return label
    return "50000+"


def split_parts(amount_paise: int, cap_paise: int | None,
                max_parts: int) -> tuple[int, ...]:
    """The presentations a debit must be broken into to clear a per-transaction ceiling.

    Returns one part when no split is needed, several when it is, and `()` when the
    amount cannot be cleared within `max_parts` — an over-cap amount that no permitted
    split reaches is not debitable at all, and saying so is the difference between the
    agent choosing a payment link and re-presenting an amount that will be refused again.

    Parts are as even as integer paise allow and sum to *exactly* `amount_paise`:
    POL-AMT-001 reconciles the parts against the cycle, so a rounding remainder left
    behind would be a debit that collects less than it owes.
    """
    if amount_paise <= 0 or max_parts < 1:
        return ()
    if cap_paise is None or amount_paise <= cap_paise:
        return (amount_paise,)
    if cap_paise <= 0:
        return ()
    n = -(-amount_paise // cap_paise)          # ceil, in integers
    if n > max_parts:
        return ()
    base, remainder = divmod(amount_paise, n)
    parts = tuple(base + 1 if i < remainder else base for i in range(n))
    return parts if max(parts) <= cap_paise else ()
