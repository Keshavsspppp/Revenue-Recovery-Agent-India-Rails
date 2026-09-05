"""Read back what actually happened at Razorpay, and write it to the ledger.

The live slice creates real objects; this closes the loop. Without it the demo can say
"the agent sent a real payment link" but never "and the customer paid it" — a link nobody
has paid produces no payment, which is exactly why the Razorpay dashboard's Payments
screen stays empty while Payment Links fills up.

Recovery is counted from what the provider confirms, never from the fact that we asked.
That is the same rule the simulator follows: `settlement_feed` is the source of truth
there too, and an accepted request is not a recovery.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.clock import IST, wall_clock
from app.domain.config import Config
from app.domain.enums import Arm, Stage, TerminalState
from app.ledger import EventDraft, Ledger


@dataclass
class SyncedLink:
    account_id: str
    provider_id: str
    status: str
    amount_paise: int
    amount_paid_paise: int
    paid_at: datetime | None
    url: str

    @property
    def settled(self) -> bool:
        return self.amount_paid_paise >= self.amount_paise > 0


def sync_live(path: str | Path, cfg: Config, adapter, *, throttle_s: float = 0.4,
              ) -> list[SyncedLink]:
    """Fetch every Razorpay object this batch created and record what became of it.

    Only new information is appended: a link that has not moved since the last sync
    writes nothing. The ledger is append-only and a second sync must not manufacture a
    second settlement for the same payment.
    """
    con = sqlite3.connect(str(path))
    batch_id = con.execute("SELECT batch_id FROM batches LIMIT 1").fetchone()[0]
    created = [(a, json.loads(p)["result"]) for a, p in con.execute(
        "SELECT account_id, payload FROM events WHERE stage=? AND json_extract("
        "payload,'$.result.provider')='razorpay_test' ORDER BY seq",
        (Stage.EXECUTE.value,))]
    # Anything already recorded as settled stays recorded once.
    already = {a for (a,) in con.execute(
        "SELECT DISTINCT account_id FROM events WHERE stage=? AND settled=1",
        (Stage.OBSERVE.value,))}
    cycles = dict(con.execute("SELECT account_id, cycle_id FROM cycles"))
    con.close()

    out: list[SyncedLink] = []
    led = Ledger(path, batch_id)
    try:
        for i, (account_id, ref) in enumerate(created):
            provider_id = ref.get("provider_id") or ""
            if not provider_id.startswith("plink_"):
                continue
            if i:
                # The list endpoint rate-limits hard; fetching one at a time with a small
                # gap is what makes a repeated sync safe to run in front of an audience.
                time.sleep(throttle_s)
            body = adapter.fetch_payment_link(provider_id)
            paid = int(body.get("amount_paid") or 0)
            amount = int(body.get("amount") or ref.get("amount_paise") or 0)
            ts = body.get("paid_at") or body.get("updated_at")
            paid_at = (datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST)
                       if paid and ts else None)
            link = SyncedLink(account_id=account_id, provider_id=provider_id,
                              status=str(body.get("status") or "unknown"),
                              amount_paise=amount, amount_paid_paise=paid,
                              paid_at=paid_at, url=str(body.get("short_url") or ""))
            out.append(link)

            if not link.settled or account_id in already:
                continue
            # A real payment at a real provider is the one case rule 6 leaves open, and
            # `wall_clock()` is its single permitted reader — the same function the
            # ledger's provenance field uses. Razorpay's own timestamp wins when it
            # gives one; this is the fallback, not the source.
            at = paid_at or wall_clock().astimezone(IST)
            led.append(EventDraft(
                stage=Stage.OBSERVE, occurred_at=at, account_id=account_id,
                cycle_id=cycles.get(account_id), arm=Arm.TREATMENT,
                result={"ok": True, "settled_source": "payment_link",
                        "provider": "razorpay_test", "provider_id": provider_id,
                        "amount_paise": amount, "collected_paise": paid,
                        "settled_at": at.isoformat()},
                evidence=("provider:razorpay_test", f"provider_id:{provider_id}",
                          f"status:{link.status}"),
                notes="confirmed paid at Razorpay; recovery counted from the provider, "
                      "never from the request"))
            led.append(EventDraft(
                stage=Stage.CLOSE, occurred_at=at, account_id=account_id,
                cycle_id=cycles.get(account_id), arm=Arm.TREATMENT,
                result={"terminal_state": TerminalState.RECOVERED.value},
                notes="closed on a confirmed Razorpay payment"))
            already.add(account_id)
    finally:
        led.close()
    return out


def render(links: list[SyncedLink], adapter: Any) -> str:
    from app.domain.money import format_inr

    out = ["RAZORPAY SYNC — what actually happened", ""]
    if not links:
        out.append("  No Razorpay objects in this batch. Run `rr live` first.")
        return "\n".join(out)
    paid = [x for x in links if x.settled]
    for x in links:
        mark = "PAID" if x.settled else x.status.upper()
        out.append(f"  {x.account_id}  {x.provider_id:<22} {mark:<9} "
                   f"{format_inr(x.amount_paid_paise):>12} of {format_inr(x.amount_paise)}")
        if not x.settled and x.url:
            out.append(f"  {'':<14} pay it: {x.url}")
    out.append("")
    out.append(f"  {len(paid)} of {len(links)} links paid, "
               f"{format_inr(sum(x.amount_paid_paise for x in links))} collected")
    if not paid:
        out.append("")
        out.append("  Nothing has been paid yet, so the dashboard's Payments screen is")
        out.append("  empty by definition. Open a link above and pay it with a test card")
        out.append("  (4111 1111 1111 1111, any future expiry, any CVV), then run this")
        out.append("  again — the payment lands in the ledger as a confirmed recovery.")
    return "\n".join(out)
