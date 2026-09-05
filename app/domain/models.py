"""Domain entities. Frozen dataclasses, never dicts. docs/01-DOMAIN-MODEL.md is the contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime

from app.domain.enums import (
    ActionType,
    Arm,
    CauseClass,
    Channel,
    MandateStatus,
    MerchantCategory,
    PTPStatus,
    Rail,
    Verdict,
)


def make_id(prefix: str, n: int) -> str:
    """Prefixed, sortable, and deterministic given the same seed.

    Deliberately not a wall-clock ULID: docs/02-LEDGER.md asks for monotonic-within-batch
    ids, and CLAUDE.md rule 5 requires a byte-identical ledger across runs of the same
    seed. A real timestamp in the id would break that. See docs/DECISIONS.md.
    """
    return f"{prefix}_{n:08d}"


def canonical_json(obj: object) -> str:
    """Sorted keys, no whitespace, UTF-8. The hashing and signing format."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_encode)


def _encode(o: object) -> object:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON-serialisable: {type(o).__name__}")


def sha256_of(obj: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConsentState:
    channels_allowed: frozenset[Channel]
    dnd_registered: bool = False           # TRAI DND — blocks promotional traffic
    opted_out_at: datetime | None = None
    recording_consent: bool = False        # required before VOICE_CONFIRM_PTP
    purpose: str = "payment_recovery"      # DPDP purpose limitation
    #: When the customer last completed an additional-factor authentication. POL-AFA-002
    #: refuses to move a mandate onto another rail without one that is still fresh —
    #: which is what makes "re-consent, then re-register" a real sequence rather than a
    #: label. docs/04 lists re-registering without fresh consent as explicitly wrong.
    afa_authorised_at: datetime | None = None


@dataclass(frozen=True)
class Account:
    account_id: str                        # acc_...
    merchant_category: MerchantCategory
    city_tier: int                         # 1 | 2 | 3 — affects rail success rates
    consent: ConsentState
    created_at: datetime


@dataclass(frozen=True)
class Mandate:
    """Mandate health is a first-class object, separate from the payment. An account may
    hold several across rails in different states — that is what makes REREGISTER_MANDATE
    a real action rather than a synonym for retry."""

    mandate_id: str                        # mnd_...
    account_id: str
    rail: Rail
    cap_paise: int
    status: MandateStatus
    registered_at: datetime
    defect: CauseClass | None = None       # why INVALID, if it is


@dataclass(frozen=True)
class BillingCycle:
    cycle_id: str                          # cyc_...
    account_id: str
    amount_paise: int
    due_date: date
    horizon_days: int = 30                 # the measurement window; fixed and stated


@dataclass(frozen=True)
class AttemptResult:
    ok: bool
    rail_code: str                         # "SUCCESS" | "AP01" | "AP66" | "U30" ...
    settled_at: datetime | None            # settlement truth, not API acknowledgement
    fee_paise: int

    def __post_init__(self) -> None:
        if self.ok and self.settled_at is None:
            raise ValueError("ok=True with settled_at=None: recovery is counted on "
                             "settlement, never on an accepted request")


@dataclass(frozen=True)
class NoticeReceipt:
    """Proof a pre-debit notice was issued. POL-NOTICE-001/002/003 read these."""

    notice_id: str
    mandate_id: str
    amount_paise: int
    issued_at: datetime
    debit_scheduled_for: datetime
    merchant_name: str
    mandate_reference: str
    opt_out_included: bool
    consumed_by_action_hash: str | None = None   # a notice is consumed by one attempt


@dataclass(frozen=True)
class Action:
    type: ActionType
    rail: Rail | None = None
    channel: Channel | None = None
    template_id: str | None = None         # DLT-registered template ref
    amount_paise: int | None = None        # filled by the executor, never by the LLM
    scheduled_for: datetime | None = None
    parts: tuple[int, ...] | None = None   # for SPLIT_DEBIT
    target_rail: Rail | None = None        # for REREGISTER_MANDATE
    disclosure: bool = False               # POL-AI-001, VOICE_CONFIRM_PTP
    cli_series: str | None = None          # POL-NUM-001: "1600" service / "140" promo
    promotional: bool = False              # nothing here is promotional; the rule says so

    def hash(self) -> str:
        """Stable sha256 over the canonical JSON. This is what the gate signs and the
        rail adapter checks. See docs/05-POLICY-ENGINE.md."""
        return sha256_of({
            "type": self.type.value,
            "rail": self.rail.value if self.rail else None,
            "channel": self.channel.value if self.channel else None,
            "template_id": self.template_id,
            "amount_paise": self.amount_paise,
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "parts": list(self.parts) if self.parts else None,
            "target_rail": self.target_rail.value if self.target_rail else None,
            "disclosure": self.disclosure,
            "cli_series": self.cli_series,
            "promotional": self.promotional,
        })


@dataclass(frozen=True)
class PromiseToPay:
    """A commitment with a verifiable outcome, not a CRM note. A broken promise is a
    stronger signal than a missed payment."""

    ptp_id: str                            # ptp_...
    account_id: str
    cycle_id: str
    amount_paise: int
    promised_date: date
    channel: Channel
    captured_by: str                       # "VOICE_CONFIRM_PTP" | "REQUEST_PTP"
    confidence: float                      # prior trust for this account, 0..1
    status: PTPStatus = PTPStatus.OPEN


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    action_hash: str
    verdict: Verdict
    rule_ids_passed: tuple[str, ...]
    rule_id_failed: str | None
    reason: str | None
    policy_version: str
    evaluated_at: datetime
    basis: str | None = None               # the regulation the failing rule implements


@dataclass(frozen=True)
class Budgets:
    """The scarce resources the planner allocates."""

    attempts_remaining: int
    contacts_remaining_week: int
    voice_remaining_cycle: int
    spend_remaining_paise: int


@dataclass(frozen=True)
class AccountFlags:
    disputed: bool = False
    subjudice: bool = False
    bereavement_at: datetime | None = None
    hardship: bool = False                 # observed exit, not latent truth
    escalated_this_cycle: bool = False      # POL-AI-004


@dataclass(frozen=True)
class InflowEstimate:
    """Derived from the timestamps of past *successful* debits — observable history only,
    never the simulator's salary date. tests/test_boundaries.py enforces that."""

    day_of_month: int | None
    concentration: float                   # 0..1, from circular variance
    n_observations: int


@dataclass(frozen=True)
class AgentState:
    """The planner's input. Nothing latent, nothing from app.sim."""

    account_id: str
    cycle: BillingCycle
    days_left: int
    arm: Arm
    cause_posterior: dict[CauseClass, float]
    mandates: tuple[Mandate, ...]
    budgets: Budgets
    notice_pending_for: datetime | None
    last_attempt_at: datetime | None
    attempts_made: int
    contacts_made: tuple[datetime, ...]
    ptp: PromiseToPay | None
    inflow_phase_estimate: InflowEstimate
    consent: ConsentState
    merchant_category: MerchantCategory
    hardship_score: float                  # 0..1, from observable signals only
    city_tier: int = 1
    flags: AccountFlags = field(default_factory=AccountFlags)
