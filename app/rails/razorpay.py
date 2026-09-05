"""Razorpay test mode, behind the same interface as the simulator.

This is the claim in `README.md` being cashed: *"Swapping the simulator for a sandbox is a
one-file change behind `RailAdapter`."* Same agent, same compliance gate, same ledger —
only the rail underneath changes.

**What is real here, and what is not.** Every id this writes to the ledger is a real
Razorpay test-mode id you can look up in the dashboard: order ids, payment ids, payment
link URLs, token ids. The error codes are Razorpay's own. What is *not* real is the
measurement — you cannot drive two thousand accounts through test mode, because mandate
authorisation needs a human in a browser and the API rate-limits long before that. So the
batch number stays simulated and this runs a small live slice beside it.

Saying which half is which, out loud, is the point. A demo that blurs them is worth less
than one that does not.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.domain.codemap import SUCCESS, CodeMap, load_codemap
from app.domain.config import Config
from app.domain.enums import ActionType, CauseClass, MandateStatus, Rail
from app.domain.models import Action, AttemptResult, Mandate, NoticeReceipt, make_id
from app.rails.base import Settlement, require_gate

BASE_URL = "https://api.razorpay.com/v1"
USER_AGENT = "revenue-recovery/0.1 (+python-urllib)"

#: Our rails, expressed as Razorpay's own method and auth type for a recurring mandate.
METHOD_FOR: dict[Rail, dict[str, str]] = {
    Rail.ENACH: {"method": "emandate", "auth_type": "netbanking"},
    Rail.UPI_AUTOPAY: {"method": "upi", "auth_type": "upi"},
    Rail.CARD_EMANDATE: {"method": "card", "auth_type": "3ds"},
}


class RazorpayUnavailable(Exception):
    """No test credentials. The simulated rails run instead — which is the default and
    what every measured number in this repo is produced on."""


class RazorpayError(Exception):
    """The API refused. Carries the body so the ledger can record what it said."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _request(method: str, url: str, auth: str, payload: dict[str, Any] | None,
             timeout: float) -> dict[str, Any]:
    """One HTTPS call via the standard library, HTTP Basic auth as Razorpay expects.

    Same reasoning as the Groq client: a handful of JSON calls does not justify a
    dependency, and adding one means the repo stops installing where it cannot be fetched.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise RazorpayError(exc.code, exc.read().decode("utf-8", "replace")) from None


@dataclass
class RazorpayTestAdapter:
    """Live Razorpay test mode. Transport is injectable so the mapping and the gate
    enforcement can be tested without credentials."""

    key_id: str
    key_secret: str
    cfg: Config
    codemap: CodeMap = field(default_factory=load_codemap)
    timeout_s: float = 15.0
    transport: Callable[..., dict[str, Any]] = _request

    calls: int = 0
    errors: int = 0
    #: Every provider id this run created, so the audit trail can be walked in the
    #: Razorpay dashboard afterwards.
    created: list[dict[str, str]] = field(default_factory=list)
    _notices: dict[str, NoticeReceipt] = field(default_factory=dict)
    _tokens: dict[str, str] = field(default_factory=dict)
    _customers: dict[str, str] = field(default_factory=dict)
    _settlements: list[Settlement] = field(default_factory=list)
    _notice_seq: int = 0

    name = "razorpay_test"

    @classmethod
    def from_env(cls, cfg: Config, **overrides: Any) -> RazorpayTestAdapter:
        import os

        # As with the Groq proposer: the CLI reads .env at entry, nothing else does.
        # A constructor that repopulates the environment can put a live credential back
        # under a test that had explicitly removed it.
        key_id = os.environ.get("RAZORPAY_KEY_ID") or ""
        secret = os.environ.get("RAZORPAY_KEY_SECRET") or ""
        if not key_id or not secret:
            raise RazorpayUnavailable(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. The simulated rails "
                "run instead — that is the default, and every measured number in this "
                "repo is produced on them.")
        if not key_id.startswith("rzp_test_"):
            raise RazorpayUnavailable(
                f"refusing to run against {key_id[:12]}...: this adapter is for test mode "
                "only. A live key would move real money on behalf of real customers.")
        return cls(key_id=key_id, key_secret=secret, cfg=cfg, **overrides)

    @property
    def _auth(self) -> str:
        return base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()

    def _call(self, method: str, path: str,
              payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        try:
            return self.transport(method, f"{BASE_URL}{path}", self._auth, payload,
                                  self.timeout_s)
        except RazorpayError:
            self.errors += 1
            raise

    def _record(self, kind: str, ident: str, **extra: str) -> None:
        self.created.append({"kind": kind, "id": ident, **extra})

    # ---- customers -----------------------------------------------------------

    def customer_for(self, account_id: str) -> str:
        """One Razorpay customer per account, created on first use.

        `fail_existing: 0` so a re-run reuses the customer rather than erroring — the
        batch is regenerated from a seed, and the same account_id must map to the same
        customer every time or the trail stops lining up.
        """
        if account_id in self._customers:
            return self._customers[account_id]
        body = self._call("POST", "/customers", {
            "name": f"Test Account {account_id}",
            "email": f"{account_id}@example.test",
            "contact": "+919000000000",
            "fail_existing": 0,
            "notes": {"account_id": account_id, "source": "revenue-recovery"},
        })
        self._customers[account_id] = body["id"]
        self._record("customer", body["id"], account_id=account_id)
        return body["id"]

    # ---- the RailAdapter interface -------------------------------------------

    def register_mandate(self, account_id: str, rail: Rail, cap_paise: int,
                         at: datetime, gate: object) -> Mandate:
        """Create a real authorisation link for an eNACH or UPI Autopay mandate.

        The customer has to complete it in a browser, which is exactly the AFA step
        POL-AFA-002 exists to insist on — so the mandate comes back PENDING_AFA here for
        the same reason it does in the simulator. The `short_url` is real and openable.
        """
        action = Action(type=ActionType.REREGISTER_MANDATE, target_rail=rail)
        require_gate(action, gate)
        method = METHOD_FOR.get(rail, METHOD_FOR[Rail.ENACH])
        body = self._call("POST", "/subscription_registration/auth_links", {
            "customer": {"name": f"Test Account {account_id}",
                         "email": f"{account_id}@example.test",
                         "contact": "+919000000000"},
            "type": "link",
            "amount": 0,
            "currency": "INR",
            "description": "Mandate authorisation",
            "subscription_registration": {
                "method": method["method"],
                "auth_type": method["auth_type"],
                "max_amount": max(cap_paise, 100),
                "expire_at": int(at.timestamp()) + 30 * 24 * 3600,
            },
            "notes": {"account_id": account_id, "rail": rail.value},
        })
        self._record("auth_link", body.get("id", ""), url=body.get("short_url", ""),
                     account_id=account_id, rail=rail.value)
        self._notice_seq += 1
        return Mandate(
            mandate_id=body.get("id") or make_id("mnd", 900_000 + self._notice_seq),
            account_id=account_id, rail=rail, cap_paise=cap_paise,
            status=MandateStatus.PENDING_AFA, registered_at=at)

    def notify(self, mandate: Mandate, action: Action, at: datetime,
               gate: object) -> tuple[NoticeReceipt, ...]:
        """The pre-debit notice.

        Razorpay has no API for this — for token-based recurring the merchant sends it,
        so the real integration would call an SMS provider here. What the adapter does is
        issue the receipt the gate and the ledger check against, which is the part that
        has to exist for POL-NOTICE-001/002/003 to mean anything.
        """
        require_gate(action, gate)
        receipts = []
        for amount_paise in (action.parts or (action.amount_paise or 0,)):
            self._notice_seq += 1
            receipt = NoticeReceipt(
                notice_id=make_id("not", self._notice_seq),
                mandate_id=mandate.mandate_id,
                amount_paise=amount_paise,
                issued_at=at,
                debit_scheduled_for=action.scheduled_for or at,
                merchant_name="Revenue Recovery Test Merchant",
                mandate_reference=mandate.mandate_id,
                opt_out_included=True)
            self._notices[receipt.notice_id] = receipt
            receipts.append(receipt)
        return tuple(receipts)

    def attempt(self, mandate: Mandate, action: Action, at: datetime,
                gate: object) -> AttemptResult:
        """Charge the mandate: create an order, then a recurring payment against it.

        The notice window is checked *before* the call, not after — presenting a debit we
        know has no live notice would be a compliance failure we paid Razorpay to tell us
        about.
        """
        require_gate(action, gate)
        return self._present(mandate, action.amount_paise or 0, at, action)

    def attempt_split(self, mandate: Mandate, action: Action, at: datetime,
                      gate: object) -> list[AttemptResult]:
        """One order and one recurring payment per part, under a single gate decision.

        Razorpay has no split primitive — nor does NPCI; a split is *n* presentations
        against the same mandate, which is exactly what this does. Each part carries its
        own notice and its own return code, and presentation stops at the first
        structural refusal rather than manufacturing a second decline the issuer never
        sent.
        """
        require_gate(action, gate)
        results: list[AttemptResult] = []
        for part in (action.parts or ()):
            result = self._present(mandate, part, at, action)
            results.append(result)
            if not result.ok and self.codemap.cause_of(result.rail_code) not in (
                    CauseClass.INSUFFICIENT_FUNDS, CauseClass.TRANSIENT_INFRA):
                break
        return results

    def _present(self, mandate: Mandate, amount: int, at: datetime,
                 action: Action) -> AttemptResult:
        if self._live_notice(mandate, amount, at) is None:
            from app.domain.codemap import NOTICE_WINDOW_VIOLATION
            return AttemptResult(False, NOTICE_WINDOW_VIOLATION, None, 0)
        self._consume_notice(mandate, amount, at, action)

        customer_id = self.customer_for(mandate.account_id)
        token = self._tokens.get(mandate.mandate_id)
        if token is None:
            # No authorised token means the mandate was never completed by the customer.
            return AttemptResult(False, self._code_for("AUTH_ARTEFACT", mandate.rail),
                                 None, 0)

        order = self._call("POST", "/orders", {
            "amount": amount, "currency": "INR", "customer_id": customer_id,
            "method": METHOD_FOR.get(mandate.rail, METHOD_FOR[Rail.ENACH])["method"],
            "receipt": f"{mandate.account_id}-{int(at.timestamp())}",
            "notes": {"account_id": mandate.account_id},
        })
        self._record("order", order["id"], account_id=mandate.account_id)

        try:
            payment = self._call("POST", "/payments/create/recurring", {
                "email": f"{mandate.account_id}@example.test",
                "contact": "+919000000000",
                "amount": amount, "currency": "INR",
                "order_id": order["id"], "customer_id": customer_id,
                "token": token, "recurring": "1",
                "description": "Recurring collection",
            })
        except RazorpayError as exc:
            return AttemptResult(False, self._map_error(exc.body, mandate.rail), None, 0)

        self._record("payment", payment.get("id", ""), account_id=mandate.account_id,
                     status=payment.get("status", ""))
        if payment.get("status") in ("captured", "authorized"):
            settled_at = datetime.fromtimestamp(
                payment.get("created_at", int(at.timestamp())), tz=timezone.utc)
            self._settlements.append(Settlement(
                account_id=mandate.account_id, cycle_id="", amount_paise=amount,
                settled_at=settled_at, source="rail",
                reference=payment.get("id", "")))
            return AttemptResult(True, SUCCESS, settled_at,
                                 self.cfg.attempt_fee_paise(mandate.rail))
        return AttemptResult(False, self._map_payment_failure(payment, mandate.rail),
                             None, 0)

    def payment_link(self, account_id: str, amount_paise: int, at: datetime,
                     action: Action, gate: object) -> dict[str, str]:
        """A real, openable payment link. The most demonstrable action in the set: the
        URL this returns works in a browser and can be paid with a test card."""
        require_gate(action, gate)
        body = self._call("POST", "/payment_links", {
            "amount": amount_paise, "currency": "INR",
            "description": "Payment for your subscription",
            "customer": {"name": f"Test Account {account_id}",
                         "email": f"{account_id}@example.test",
                         "contact": "+919000000000"},
            "notify": {"sms": False, "email": False},   # we send via our own DLT template
            "reminder_enable": False,
            "notes": {"account_id": account_id},
        })
        self._record("payment_link", body.get("id", ""), url=body.get("short_url", ""),
                     account_id=account_id)
        return {"id": body.get("id", ""), "url": body.get("short_url", "")}

    def fetch_payment_link(self, provider_id: str) -> dict[str, Any]:
        """What became of a link we created. Read-only.

        Recovery is counted from what the provider confirms, never from the fact that we
        asked — the same rule the simulator's settlement feed follows.
        """
        return self._call("GET", f"/payment_links/{provider_id}")

    def mandate_status(self, mandate_id: str) -> MandateStatus:
        return (MandateStatus.ACTIVE if mandate_id in self._tokens
                else MandateStatus.PENDING_AFA)

    def settlement_feed(self, since: datetime) -> list[Settlement]:
        return [s for s in self._settlements if s.settled_at >= since]

    # ---- mapping Razorpay's answers onto our taxonomy ------------------------

    def _map_error(self, body: str, rail: Rail) -> str:
        try:
            error = json.loads(body).get("error", {})
        except ValueError:
            return "UNKNOWN"
        return self._razorpay_code(error.get("reason") or error.get("code") or "", rail)

    def _map_payment_failure(self, payment: dict[str, Any], rail: Rail) -> str:
        return self._razorpay_code(
            payment.get("error_reason") or payment.get("error_code") or "", rail)

    def _razorpay_code(self, reason: str, rail: Rail) -> str:
        """Razorpay's reason strings onto our cause taxonomy, via config.

        Deliberately a lookup in `config/codemap.yaml` rather than a match statement
        here: the whole premise of `04-CAUSE-TAXONOMY.md` is that these lists drift, and
        the one thing that must not require a code change is a provider renaming a code.
        """
        from app.domain.enums import CauseClass

        mapped = self.codemap.provider_cause("razorpay", reason)
        if mapped is CauseClass.UNKNOWN:
            # A reason we have never seen. Return it verbatim, prefixed, rather than
            # forcing it onto a rail code we would be inventing: `cause_of` will read it
            # as UNKNOWN and `is_unmapped` will count it, which is how taxonomy drift
            # becomes a number on the scoreboard instead of a silent mis-diagnosis.
            #
            # This also stops a crash. `code_for(rail, UNKNOWN)` has no answer and raises,
            # so before this the first unrecognised code Razorpay returned would have
            # taken the run down.
            return f"RZP:{reason or 'unspecified'}"
        return self.codemap.code_for(rail, mapped)

    def _code_for(self, cause_name: str, rail: Rail) -> str:
        from app.domain.enums import CauseClass
        return self.codemap.code_for(rail, CauseClass(cause_name))

    # ---- notice bookkeeping, identical to the simulator ----------------------

    def _live_notice(self, mandate: Mandate, amount_paise: int,
                     at: datetime) -> NoticeReceipt | None:
        from datetime import timedelta
        hours = self.cfg.notice_hours(mandate.rail)
        for receipt in self._notices.values():
            if (receipt.mandate_id == mandate.mandate_id
                    and receipt.amount_paise == amount_paise
                    and receipt.consumed_by_action_hash is None
                    and at - receipt.issued_at >= timedelta(hours=hours)):
                return receipt
        return None

    def _consume_notice(self, mandate: Mandate, amount_paise: int, at: datetime,
                        action: Action) -> None:
        receipt = self._live_notice(mandate, amount_paise, at)
        if receipt is not None:
            self._notices[receipt.notice_id] = NoticeReceipt(
                **{**receipt.__dict__, "consumed_by_action_hash": action.hash()})

    def notice_for(self, mandate: Mandate, amount_paise: int,
                   at: datetime) -> NoticeReceipt | None:
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
        return self._notices.get(receipt.notice_id, receipt)

    def _live_notices(self, mandate: Mandate, amount_paise: int, at: datetime):
        from datetime import timedelta

        hours = self.cfg.notice_hours(mandate.rail)
        return [r for r in self._notices.values()
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


    def stats(self) -> dict[str, Any]:
        return {"adapter": self.name, "calls": self.calls, "errors": self.errors,
                "created": len(self.created),
                "kinds": sorted({c["kind"] for c in self.created})}
