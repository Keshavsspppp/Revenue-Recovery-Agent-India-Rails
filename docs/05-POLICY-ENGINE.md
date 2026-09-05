# 05 — Policy engine

Module: `app/policy/`

"You must not call customers after 7pm" in a system prompt is a suggestion. It will hold
ninety-nine times and fail on the hundredth, and the hundredth is the one on the
recording. Compliance belongs in a **pre-flight gate**, in code, that every action passes
through — and whose verdicts, pass or fail, are written to the ledger.

## Contract

One pure function. No I/O, no clock reads, no randomness.

```python
def evaluate(action: Action,
             ctx: GateContext,
             policy: PolicySet) -> GateDecision
```

```python
@dataclass(frozen=True)
class GateContext:
    now: datetime                  # simulated, IST
    account: Account
    consent: ConsentState
    mandates: tuple[Mandate, ...]
    cycle: BillingCycle
    budgets: Budgets
    contacts_made: tuple[datetime, ...]
    notices: tuple[NoticeReceipt, ...]
    ptp: PromiseToPay | None
    flags: AccountFlags            # disputed, subjudice, bereavement, hardship
    calendar: Calendar             # festivals, regional holidays
```

Rules are declarative, versioned, and ordered. The first `DENY` short-circuits; every
rule evaluated before it is recorded in `rule_ids_passed`.

```python
@dataclass(frozen=True)
class Rule:
    rule_id: str          # POL-QH-001
    title: str
    basis: str            # the regulation or policy it implements
    applies_to: frozenset[ActionType]
    check: Callable[[Action, GateContext], bool]
    deny_reason: str
```

`policy_version` is a string like `pol_2026.09.1` written into every `GATE` and
`EXECUTE` event. Changing any rule bumps it. A trail that cannot name the policy version
in force is not a defence.

---

## Rule catalogue

Implement all of these. Each gets a unit test named after its ID.

### Absolute stops — evaluated first, deny everything

| ID | Rule | Basis |
|---|---|---|
| `POL-STOP-001` | If `consent.opted_out_at` is set, deny all outreach actions | TCCCPR / DPDP |
| `POL-STOP-002` | If `flags.disputed`, deny everything except `ESCALATE_HUMAN`, `CLOSE` | RBI conduct |
| `POL-STOP-003` | If `flags.subjudice`, deny all recovery actions | RBI draft recovery norms — no recovery while the matter is subjudice |
| `POL-STOP-004` | If `flags.bereavement` within `config.bereavement_days` (30), deny all contact | RBI draft norms — no contact during bereavement or family calamity |
| `POL-STOP-005` | If terminal state already written for this cycle, deny everything | ledger invariant 6 |

### Contact-timing rules

| ID | Rule | Basis |
|---|---|---|
| `POL-QH-001` | Contact actions (`SEND_MESSAGE`, `REQUEST_PTP`, `VOICE_CONFIRM_PTP`, `SEND_PAYMENT_LINK`) only between **08:00 and 19:00 IST** | RBI draft recovery norms — 8am–7pm calling window |
| `POL-QH-002` | `VOICE_CONFIRM_PTP` additionally denied on Sundays and gazetted holidays unless `config.voice.allow_holidays` | conservative reading of "decency and decorum" |
| `POL-QH-003` | Deny contact on dates in `calendar.festivals` for the account's region | RBI draft norms — no contact during festivals |

> `SEND_PREDEBIT_NOTICE` is deliberately **exempt** from `POL-QH-001`: it is a
> regulatory notification, not a recovery contact. Encode the exemption explicitly with
> a comment, because it will be questioned — and it is the kind of nuance that shows you
> read the rules rather than pattern-matched them.

### Debit rules

| ID | Rule | Basis |
|---|---|---|
| `POL-NOTICE-001` | `RETRY_DEBIT` requires a `NoticeReceipt` for this mandate+amount issued ≥ `rail.notice_hours` (24) before `action.scheduled_for` ⚠️ | RBI e-mandate framework — pre-debit notification |
| `POL-NOTICE-002` | The notice must carry merchant name, amount, debit date, mandate reference and an opt-out. Deny if any field is absent | RBI e-mandate framework |
| `POL-NOTICE-003` | A notice is consumed by one attempt. A second attempt needs a second notice ⚠️ | see verification note below |
| `POL-AFA-001` | Deny `RETRY_DEBIT` where `amount > afa_free_cap` for the rail, unless `merchant_category ∈ {INSURANCE, MUTUAL_FUND, CREDIT_CARD_BILL}` and `amount ≤ high_cap` | RBI e-mandate — ₹15,000 / ₹1,00,000 thresholds |
| `POL-AFA-002` | `REREGISTER_MANDATE` requires an AFA step; deny if `ctx.consent` has no fresh customer authorisation | RBI e-mandate — AFA on registration, modification, withdrawal |
| `POL-AMT-001` | Deny any debit where `amount != cycle.amount_paise` unless the action is `SPLIT_DEBIT` and `sum(parts) == cycle.amount_paise` | internal integrity |

> ⚠️ `POL-NOTICE-003` encodes the *per-retry fresh notice* reading. It is the load-bearing
> assumption of the whole project. If you verify that re-presentations are exempt,
> change this rule and **re-run the batch** — the attempt budget loosens and your
> headline number changes. Do not quietly leave it as-is.

### Channel and messaging rules

| ID | Rule | Basis |
|---|---|---|
| `POL-CONSENT-001` | Deny any contact on a channel not in `consent.channels_allowed` | DPDP consent |
| `POL-DLT-001` | `SEND_MESSAGE` requires a `template_id` present in the DLT template registry | TRAI TCCCPR / DLT registration |
| `POL-DLT-002` | Deny free-text customer messaging. Body is rendered from the registered template only | TRAI + invariant 4 |
| `POL-NUM-001` | Voice and service messaging must use a **1600-series** CLI for transactional/service contact by a regulated entity to an existing customer; promotional traffic must use **140** and is denied if `consent.dnd_registered` | TRAI 1600/140 clarification |
| `POL-PURPOSE-001` | Deny if `consent.purpose != "payment_recovery"` | DPDP purpose limitation |

### Frequency, fatigue and budget rules

| ID | Rule | Basis |
|---|---|---|
| `POL-FREQ-001` | ≤ `config.max_contacts_per_week` (3) contact actions per account per rolling 7 days | RBI draft norms — no excessive calling |
| `POL-FREQ-002` | ≤ 1 contact action per account per calendar day | conduct |
| `POL-FREQ-003` | ≤ `config.max_voice_per_cycle` (1) `VOICE_CONFIRM_PTP` per cycle | conduct |
| `POL-BUDGET-001` | Deny if `budgets.attempts_remaining <= 0` | internal |
| `POL-BUDGET-002` | Deny if action cost > `budgets.spend_remaining_paise` | internal |
| `POL-PTP-001` | If a `PromiseToPay` is `OPEN`, deny all actions except `WAIT` until `promised_date + grace_days` | conduct + design |

### AI-specific rules

| ID | Rule | Basis |
|---|---|---|
| `POL-AI-001` | `VOICE_CONFIRM_PTP` must carry `disclosure=True` — the call opens by identifying itself as an automated agent and offering an immediate opt-out | RBI FREE-AI: "disclosure and the right to override" |
| `POL-AI-002` | `VOICE_CONFIRM_PTP` requires `consent.recording_consent`, and the call must be recorded with prior intimation | RBI draft norms — recorded with prior intimation |
| `POL-AI-003` | Any action carrying a `human_override` bypasses proposer selection but **still passes the full gate**. A human can override the agent; a human cannot override compliance | FREE-AI: "accountability regardless of autonomy" |
| `POL-AI-004` | Deny `ESCALATE_HUMAN` → auto-close loops: an account escalated to a human cannot be re-entered into automation in the same cycle | conduct |

---

## Enforcement — the gate is the only path to a rail

Do not enforce this by convention. Enforce it in the adapter:

```python
def attempt(self, mandate, amount_paise, at, gate: GateDecision) -> AttemptResult:
    if gate.verdict is not Verdict.ALLOW:
        raise GateViolation(gate.rule_id_failed)
    if gate.action_hash != self._pending_action_hash:
        raise GateViolation("action_hash mismatch")
    ...
```

Ledger invariant 4 (`02-LEDGER.md`) is the machine-checkable statement of the same fact,
verified after the run. Belt and braces, deliberately.

---

## Voice — the narrow flow

Voice is in the brief and it demos beautifully and it will eat your entire build:
code-switched Hinglish ASR, latency budgets that make barge-in feel broken, TTS that
mangles amounts and dates, plus consent, disclosure, DLT and number-series registration.

If you build it at all, build exactly one job: **confirming a promise-to-pay** on
accounts that already reached that stage.

- Bounded outcomes: `PTP_CAPTURED`, `DISPUTE`, `HARDSHIP`, `CALLBACK`, `NO_ANSWER`.
- Opens with the AI disclosure line (`POL-AI-001`) and an immediate opt-out path.
- Recorded with prior intimation (`POL-AI-002`).
- DTMF fallback so the flow degrades gracefully when ASR fails.
- Any distress or dispute language ends the call and routes to a human — a **rule**,
  not a model output.
- Transcript, outcome and handoff all written to the ledger.

**If voice is not comfortably working by the halfway mark, cut it.** A well-measured
messaging-plus-retry agent beats a voice demo that fails live.

---

## Demo the denial

Build `POST /policy/evaluate` (`09-API.md`) so you can attempt a **19:30 voice call live
on stage** and show the gate refusing it with `POL-QH-001`, then show the same denial in
the ledger. Thirty seconds. It is worth more than any recovery number, because it is the
only part of the demo that proves the system has a *floor* rather than a good intention.

---

## Tests

One test per rule ID, plus:

- `test_gate_is_pure` — same inputs, same output; no clock or RNG access.
- `test_first_deny_short_circuits` — later rules are not evaluated, and `rule_ids_passed`
  contains exactly the rules checked before the failure.
- `test_all_actions_covered` — every `ActionType` is matched by at least one rule's
  `applies_to`, so no action can slip through unexamined.
- `test_policy_version_bumps` — a hash over the rule set must equal the declared
  `policy_version`; CI fails if a rule changed without a version bump.
