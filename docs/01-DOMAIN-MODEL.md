# 01 — Domain model

This is the shared contract. Every other doc refers to these names. If you change a
name here, change it everywhere; do not introduce a synonym.

Module: `app/domain/`

---

## Primitive conventions

| Concern | Rule |
|---|---|
| Money | **Integer paise.** Field names end in `_paise`. Never a float. `149900` = ₹1,499.00 |
| Time | ISO-8601 with `+05:30`. All compliance logic evaluates in **IST**. |
| Clock | One `Clock` object (`app/domain/clock.py`). Nothing else calls `datetime.now()`. |
| IDs | Prefixed, sortable. `acc_`, `mnd_`, `cyc_`, `dec_`, `evt_`, `obs_`, `ptp_`, `bat_` |
| Randomness | One seeded `numpy.random.Generator` per batch, passed explicitly. No global RNG. |
| Enums | `str, Enum` so they serialise directly into the ledger JSON. |

```python
class Money(int):
    """Paise. Use Money.rupees(1499.00) at the edge only."""
```

---

## Enums

### Rail

```python
class Rail(str, Enum):
    UPI_AUTOPAY  = "UPI_AUTOPAY"
    ENACH        = "ENACH"
    CARD_EMANDATE= "CARD_EMANDATE"
    PAYMENT_LINK = "PAYMENT_LINK"   # not a mandate; one-time, customer-initiated
```

Rail properties live in `config/default.yaml` under `rails:`:

| Rail | `afa_free_cap_paise` | `high_cap_paise` (approved categories) | `notice_hours` | `attempt_fee_paise` | mandate required |
|---|---|---|---|---|---|
| `UPI_AUTOPAY` | 1_500_000 | 10_000_000 | 24 | 200 | yes |
| `ENACH` | 1_500_000 | 10_000_000 | 24 | 250 | yes |
| `CARD_EMANDATE` | 1_500_000 | 10_000_000 | 24 | 300 | yes |
| `PAYMENT_LINK` | — | — | 0 | 400 | no |

`high_cap_paise` applies only when `merchant.category in {INSURANCE, MUTUAL_FUND, CREDIT_CARD_BILL}`.
Above the applicable cap, the debit requires AFA and cannot run unattended.

### CauseClass

The canonical failure taxonomy. Layer 1 maps raw rail codes onto this; Layer 2 produces
a posterior over it. See `04-CAUSE-TAXONOMY.md`.

```python
class CauseClass(str, Enum):
    TRANSIENT_INFRA    = "TRANSIENT_INFRA"     # bank/rail timeout — retry, say nothing
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # timing problem, not a message problem
    LIMIT_EXCEEDED     = "LIMIT_EXCEEDED"      # over AFA cap / mandate cap — split or re-register
    AUTH_ARTEFACT      = "AUTH_ARTEFACT"       # OTP/registration artefact — customer action needed
    MANDATE_INVALID    = "MANDATE_INVALID"     # structurally wrong mandate — repair on another rail
    MANDATE_REVOKED    = "MANDATE_REVOKED"     # customer cancelled — re-consent or stop
    ACCOUNT_TERMINAL   = "ACCOUNT_TERMINAL"    # closed/frozen — terminal for this rail
    UNKNOWN            = "UNKNOWN"
```

### ActionType

The closed action space. Nothing outside this set can be scheduled.

```python
class ActionType(str, Enum):
    SEND_PREDEBIT_NOTICE = "SEND_PREDEBIT_NOTICE"
    RETRY_DEBIT          = "RETRY_DEBIT"
    SEND_MESSAGE         = "SEND_MESSAGE"
    SEND_PAYMENT_LINK    = "SEND_PAYMENT_LINK"
    REREGISTER_MANDATE   = "REREGISTER_MANDATE"
    SPLIT_DEBIT          = "SPLIT_DEBIT"
    REQUEST_PTP          = "REQUEST_PTP"
    VOICE_CONFIRM_PTP    = "VOICE_CONFIRM_PTP"
    OFFER_ACCOMMODATION  = "OFFER_ACCOMMODATION"
    ESCALATE_HUMAN       = "ESCALATE_HUMAN"
    WAIT                 = "WAIT"
    CLOSE                = "CLOSE"
```

`WAIT` is a real action with a real expected value. For the `INSUFFICIENT_FUNDS` class
it is frequently the highest-EV choice. An agent that cannot choose to do nothing is
firing, not optimising.

### TerminalState

```python
class TerminalState(str, Enum):
    RECOVERED          = "RECOVERED"           # settlement confirmed, not an API 200
    TERMINAL_RAIL      = "TERMINAL_RAIL"       # no viable rail remains
    OPTED_OUT          = "OPTED_OUT"           # absolute stop on outreach
    DISPUTED           = "DISPUTED"            # human only, automation stops
    HARDSHIP           = "HARDSHIP"            # exited into accommodation
    FATIGUE_EXHAUSTED  = "FATIGUE_EXHAUSTED"   # contact budget spent for the window
    EV_BELOW_THRESHOLD = "EV_BELOW_THRESHOLD"  # best remaining action has negative EV
    CYCLE_ENDED        = "CYCLE_ENDED"         # horizon reached, unrecovered
```

`PROMISE_ACTIVE` is deliberately **not** terminal — an open promise pauses the loop, it
does not end it. See `PromiseToPay` below.

### Stage

Ledger stage markers, in causal order.

```python
class Stage(str, Enum):
    DETECT   = "DETECT"
    ASSIGN   = "ASSIGN"     # arm assignment — must be the second event for an account
    DIAGNOSE = "DIAGNOSE"
    ELIGIBLE = "ELIGIBLE"
    PROPOSE  = "PROPOSE"
    GATE     = "GATE"
    EXECUTE  = "EXECUTE"
    OBSERVE  = "OBSERVE"
    CLOSE    = "CLOSE"
```

### Arm, Channel, Verdict

```python
class Arm(str, Enum):
    TREATMENT = "treatment"
    HOLDOUT   = "holdout"

class Channel(str, Enum):
    SMS = "SMS"; WHATSAPP = "WHATSAPP"; EMAIL = "EMAIL"; VOICE = "VOICE"; PUSH = "PUSH"

class Verdict(str, Enum):
    ALLOW = "ALLOW"; DENY = "DENY"
```

### MerchantCategory

```python
class MerchantCategory(str, Enum):
    SUBSCRIPTION     = "SUBSCRIPTION"      # OTT, SaaS — small ticket, high volume
    LENDING_EMI      = "LENDING_EMI"       # NBFC EMI — large ticket
    INSURANCE        = "INSURANCE"         # high AFA-free cap
    MUTUAL_FUND      = "MUTUAL_FUND"       # high AFA-free cap
    CREDIT_CARD_BILL = "CREDIT_CARD_BILL"  # high AFA-free cap
    UTILITY          = "UTILITY"
```

Category matters for two reasons: it selects the AFA-free cap, and it changes the harm
weighting — chasing a ₹299 OTT subscription and a ₹42,000 EMI are not the same act.

---

## Entities

All frozen dataclasses in `app/domain/models.py` unless noted.

### Account

```python
@dataclass(frozen=True)
class Account:
    account_id: str                 # acc_...
    merchant_category: MerchantCategory
    city_tier: int                  # 1 | 2 | 3 — affects rail success rates
    consent: ConsentState
    created_at: datetime
```

The account's *latent* state (balance, salary date, intent, annoyance) lives only in
the simulator and is **never** visible to the agent. Enforce this with module
boundaries: `app/sim/` may import `app/domain/`, but `app/plan/`, `app/diagnose/` and
`app/propose/` must never import `app/sim/`. Add a test that asserts this.

### Mandate

```python
@dataclass(frozen=True)
class Mandate:
    mandate_id: str                 # mnd_...
    account_id: str
    rail: Rail
    cap_paise: int                  # registered mandate ceiling
    status: MandateStatus           # ACTIVE | INVALID | REVOKED | PENDING_AFA
    registered_at: datetime
    defect: CauseClass | None       # why INVALID, if it is
```

**Mandate health is a first-class object, separate from the payment.** An account may
hold several mandates across rails in different states. This is what makes
`REREGISTER_MANDATE` a meaningful action instead of a synonym for retry.

### BillingCycle

```python
@dataclass(frozen=True)
class BillingCycle:
    cycle_id: str                   # cyc_...
    account_id: str
    amount_paise: int
    due_date: date
    horizon_days: int = 30          # the measurement window; fix it and state it
```

### Attempt / AttemptResult

```python
@dataclass(frozen=True)
class AttemptResult:
    ok: bool
    rail_code: str                  # "SUCCESS" | "AP01" | "AP66" | "U30" ...
    settled_at: datetime | None     # settlement truth, not API acknowledgement
    fee_paise: int
```

`ok=True` with `settled_at=None` is invalid and must raise. Recovery is counted on
settlement, never on an accepted request.

### Action

```python
@dataclass(frozen=True)
class Action:
    type: ActionType
    rail: Rail | None = None
    channel: Channel | None = None
    template_id: str | None = None      # DLT-registered template ref
    amount_paise: int | None = None     # filled by executor, never by the LLM
    scheduled_for: datetime | None = None
    parts: tuple[int, ...] | None = None   # for SPLIT_DEBIT
    target_rail: Rail | None = None        # for REREGISTER_MANDATE

    def hash(self) -> str: ...          # stable sha256 over the canonical JSON
```

`Action.hash()` is what the gate signs and the adapter checks. See `05-POLICY-ENGINE.md`.

### ActionCost

Every action type must have both entries in `config/default.yaml`. An action with no
cost and no harm weight cannot be scheduled — the planner cannot price it.

```yaml
action_costs:                 # paise
  SEND_PREDEBIT_NOTICE: 15
  RETRY_DEBIT:          200   # overridden by rail.attempt_fee_paise
  SEND_MESSAGE:         { SMS: 15, WHATSAPP: 35, EMAIL: 2, PUSH: 1 }
  SEND_PAYMENT_LINK:    40
  REREGISTER_MANDATE:   150
  SPLIT_DEBIT:          400   # two attempt fees
  REQUEST_PTP:          35
  VOICE_CONFIRM_PTP:    360   # ~3 min at ₹1.20/min
  OFFER_ACCOMMODATION:  0
  ESCALATE_HUMAN:       4500  # 5 min of a human at ₹9/min
  WAIT:                 0
  CLOSE:                0

harm_weights:                 # dimensionless; multiplied by lambda in the objective
  SEND_PREDEBIT_NOTICE: 0.2   # it is a contact, and it is mandatory — price it low
  RETRY_DEBIT:          0.1
  SEND_MESSAGE:         { SMS: 0.5, WHATSAPP: 0.6, EMAIL: 0.2, PUSH: 0.3 }
  SEND_PAYMENT_LINK:    0.4
  REREGISTER_MANDATE:   0.7   # asks the customer to do work
  SPLIT_DEBIT:          0.3
  REQUEST_PTP:          0.6
  VOICE_CONFIRM_PTP:    2.0   # the most intrusive thing in the set
  OFFER_ACCOMMODATION:  0.0
  ESCALATE_HUMAN:       0.5
  WAIT:                 0.0
  CLOSE:                0.0
```

> These numbers are **declared assumptions**, not measurements. Say so on the slide.
> What matters is that harm is *priced* rather than mentioned, and that varying λ
> produces a visible frontier (`08-EVALUATION.md` §λ frontier).

### ConsentState

```python
@dataclass(frozen=True)
class ConsentState:
    channels_allowed: frozenset[Channel]
    dnd_registered: bool               # TRAI DND — blocks promotional traffic
    opted_out_at: datetime | None
    recording_consent: bool            # required before VOICE_CONFIRM_PTP
    purpose: str = "payment_recovery"  # DPDP purpose limitation
```

### PromiseToPay

A promise is a commitment with a verifiable outcome, not a CRM note. A **broken**
promise is a stronger signal than a missed payment.

```python
@dataclass(frozen=True)
class PromiseToPay:
    ptp_id: str                     # ptp_...
    account_id: str
    cycle_id: str
    amount_paise: int
    promised_date: date
    channel: Channel
    captured_by: str                # "VOICE_CONFIRM_PTP" | "REQUEST_PTP"
    confidence: float               # prior trust for this account, 0..1
    status: PTPStatus               # OPEN | KEPT | BROKEN | PARTIAL | LAPSED

class PTPStatus(str, Enum):
    OPEN = "OPEN"; KEPT = "KEPT"; BROKEN = "BROKEN"
    PARTIAL = "PARTIAL"; LAPSED = "LAPSED"
```

Rules, enforced in the gate and the planner:

- An `OPEN` promise **suppresses all outreach** until `promised_date + grace_days` (default 1).
- Resolution is automatic against settlement data on `promised_date + grace_days`:
  full settlement → `KEPT`; partial → `PARTIAL`; nothing → `BROKEN`; cycle ended first → `LAPSED`.
- `KEPT` resets the contact-fatigue budget for the window.
- `BROKEN` escalates one step and lowers `confidence` for future promises from that account.

### GateDecision

```python
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
```

### Budgets

The scarce resources the planner allocates.

```python
@dataclass(frozen=True)
class Budgets:
    attempts_remaining: int          # per cycle, default 4
    contacts_remaining_week: int     # per account per week, default 3
    voice_remaining_cycle: int       # default 1
    spend_remaining_paise: int       # per account per cycle
```

Batch-level circuit breaker, checked by the runner between accounts: if
`opt_out_rate > config.circuit_breaker.opt_out_rate` (default 0.02) or
`complaint_rate > 0.005`, halt the batch and write a `CIRCUIT_BREAKER_TRIPPED` event.

---

## The account state the agent may see

The planner's input. Nothing latent, nothing from `app/sim/`.

```python
@dataclass(frozen=True)
class AgentState:
    account_id: str
    cycle: BillingCycle
    days_left: int
    arm: Arm
    cause_posterior: dict[CauseClass, float]
    mandates: tuple[Mandate, ...]
    budgets: Budgets
    notice_pending_for: datetime | None    # a notice sent, attempt not yet due
    last_attempt_at: datetime | None
    attempts_made: int
    contacts_made: tuple[datetime, ...]
    ptp: PromiseToPay | None
    inflow_phase_estimate: InflowEstimate  # see 06-PLANNER.md
    consent: ConsentState
    merchant_category: MerchantCategory
    hardship_score: float                  # 0..1, from observable signals only
```

`inflow_phase_estimate` is derived from the timestamps of the account's **past
successful debits** — observable history, not the simulator's salary date. Keep that
distinction rigorous; leaking the true salary date into the planner is the single
easiest way to accidentally fake your result.
