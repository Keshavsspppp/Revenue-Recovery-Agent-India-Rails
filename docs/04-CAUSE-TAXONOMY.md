# 04 — Cause taxonomy

Module: `app/diagnose/`

Return codes are the only diagnosis you get, and they are unreliable. NPCI rewrote the
NACH e-mandate rejection codes in circular **NPCI/2024-25/NACH/006 (27 Nov 2024,
effective 1 Jan 2025)**: 20 codes added, 33 descriptions revised, 22 removed. That churn
is itself the tell — the taxonomy is coarse, sponsor banks populate it inconsistently,
and the same underlying reality arrives under different codes from different banks.

Worse, the codes conflate categories that demand *opposite* responses. Treating them as
one bucket called "failure" is the single most common design error in this space.

---

## Layer 1 — deterministic code map

A **versioned lookup table**, not model output. `app/diagnose/codemap.py`, backed by
`config/codemap.yaml` so it can be updated without a code change.

```python
@dataclass(frozen=True)
class CodeMapping:
    code: str
    rail: Rail
    cause: CauseClass
    retryable: bool          # is a same-rail retry ever sensible
    customer_action: bool    # does fixing it require the customer to do something
    terminal_for_rail: bool
    description: str
```

### eNACH / e-mandate (AP series)

| Code | Meaning | CauseClass | Retryable | Customer action | Terminal for rail |
|---|---|---|---|---|---|
| `AP01` | Insufficient funds | `INSUFFICIENT_FUNDS` | ✅ | ❌ | ❌ |
| `AP02` | Account closed | `ACCOUNT_TERMINAL` | ❌ | ✅ | ✅ |
| `AP03` | Account frozen / blocked | `ACCOUNT_TERMINAL` | ❌ | ✅ | ✅ |
| `AP14` | Invalid net-banking credentials | `AUTH_ARTEFACT` | ❌ | ✅ | ❌ |
| `AP16` | Mandate not allowed — minor account | `MANDATE_INVALID` | ❌ | ✅ | ✅ |
| `AP17` | Mandate not allowed — NRE account | `MANDATE_INVALID` | ❌ | ✅ | ✅ |
| `AP18` | Mandate not allowed — credit card account | `MANDATE_INVALID` | ❌ | ✅ | ✅ |
| `AP19` | Mandate not allowed — PF account | `MANDATE_INVALID` | ❌ | ✅ | ✅ |
| `AP20` | Mandate not allowed — PPF account | `MANDATE_INVALID` | ❌ | ✅ | ✅ |
| `AP34` | Account not registered for net banking | `AUTH_ARTEFACT` | ❌ | ✅ | ❌ |
| `AP39` | Invalid bank OTP | `AUTH_ARTEFACT` | ✅ | ✅ | ❌ |
| `AP51` | Aadhaar not linked to account | `AUTH_ARTEFACT` | ❌ | ✅ | ❌ |
| `AP53` | Mandate cancelled by customer | `MANDATE_REVOKED` | ❌ | ✅ | ✅ |
| `AP58` | Amount exceeds mandate / AFA limit | `LIMIT_EXCEEDED` | ✅ | ❌ | ❌ |
| `AP65` | Account–debit card mismatch | `MANDATE_INVALID` | ❌ | ✅ | ❌ |
| `AP66` | Bank response timeout | `TRANSIENT_INFRA` | ✅ | ❌ | ❌ |
| `AP67` | Mobile number not in bank CBS, OTP cannot be triggered | `AUTH_ARTEFACT` | ❌ | ✅ | ❌ |
| `AP69` | Customer ID validation failure | `AUTH_ARTEFACT` | ❌ | ✅ | ❌ |
| `AP70` | UIDAI OTP expired | `AUTH_ARTEFACT` | ✅ | ✅ | ❌ |

> ⚠️ Code numbers and descriptions are drawn from published summaries of the NPCI
> circular. **Reconcile against your PSP's own code list before quoting them as
> authoritative** — the whole point of this section is that these lists drift.

### UPI Autopay (illustrative)

| Code | Meaning | CauseClass |
|---|---|---|
| `U16` | Risk threshold exceeded | `TRANSIENT_INFRA` |
| `U30` | Debit failure at remitter | `TRANSIENT_INFRA` |
| `U54` | Mandate expired | `MANDATE_REVOKED` |
| `U69` | Mandate not found / revoked | `MANDATE_REVOKED` |
| `ZM` | Invalid MPIN | `AUTH_ARTEFACT` |
| `Z9` | Insufficient funds | `INSUFFICIENT_FUNDS` |
| `NOTICE_WINDOW_VIOLATION` | Rejected at rail — no valid 24h notice | `UNKNOWN` (**your bug**, not the customer's) |

`NOTICE_WINDOW_VIOLATION` must appear on the scoreboard as a **defect counter**, not as
a customer failure. If it is ever non-zero, the scheduler has a bug. Failing loudly on
your own compliance mistake is more convincing than never making one.

### Unmapped codes

Any code not in the table maps to `UNKNOWN` and increments `unmapped_code_count`.
Report that count. A taxonomy that silently swallows unknowns is how the drift problem
hides.

---

## Layer 2 — cause posterior

True cause is **latent**. Layer 1 gives one noisy observation of it. Layer 2 combines
that with the account's history to produce a distribution.

```python
def posterior(code: str, rail: Rail, history: AccountHistory,
              at: datetime) -> dict[CauseClass, float]:
```

### Features (all observable — none from `app/sim/`)

| Feature | Signal it carries |
|---|---|
| `code_prior` | Layer 1 mapping as a soft prior (0.75 on the mapped class, rest spread) |
| `attempts_this_cycle` | Repeated `AP01` shifts weight from timing toward hardship |
| `days_since_last_success` | Long gap raises `MANDATE_*` and `ACCOUNT_TERMINAL` |
| `hour_of_day_bucket` | 19:00–22:00 raises `TRANSIENT_INFRA` |
| `amount_vs_prior_success_max` | Ratio > 1 raises `LIMIT_EXCEEDED` |
| `city_tier` | Raises `TRANSIENT_INFRA` baseline |
| `other_mandate_failing` | Same-window failure of a second mandate raises hardship / `ACCOUNT_TERMINAL` |
| `code_changed_between_attempts` | Instability raises `TRANSIENT_INFRA` and `UNKNOWN` |
| `days_since_mandate_registration` | Very recent registration raises `AUTH_ARTEFACT` |

### Model

Start with **multinomial logistic regression** (scikit-learn) trained on the simulator's
`latent_truth` labels for a *training* batch, then frozen and applied to a *held-out
evaluation* batch. Two rules:

1. **Never train and evaluate on the same batch.** Generate `batch_train` (seed 1) and
   `batch_eval` (seed 42) separately.
2. Ship the Layer 1 map even if Layer 2 never lands. A deterministic map plus a
   sensible action matrix is a complete, defensible system. Layer 2 is an upgrade.

If you prefer no ML at all, a hand-written Bayesian update over the same features is
fine and arguably easier to defend. Say which you chose and why.

---

## Validation — score decisions, not labels

You have no ground truth for "true cause" in production. You *do* have ground truth for
"did the chosen intervention work." So:

> **The cause model is scored by the recovery rate of the actions it triggers, not by
> classification accuracy.**

Report both, and be explicit that the second is the one that matters:

| Metric | Where it comes from |
|---|---|
| `cause_accuracy` | vs `latent_truth`. Available only in simulation. Report as a sanity check. |
| `action_hit_rate` | P(recovery \| action chosen by this posterior), by cause class |
| `counterfactual_regret` | Recovery under the chosen action vs the best action in hindsight, per cause class |

`counterfactual_regret` is computable in simulation because you can replay the same
seeded world under a different action. Doing that replay is a strong, cheap result and
almost nobody does it.

---

## Cause → eligible action matrix

This is the gate between diagnosis and proposal. The **planner** picks among eligible
actions; the **LLM** may only choose from what this produces. Nothing outside it is
schedulable.

| CauseClass | Eligible actions | Explicitly wrong |
|---|---|---|
| `TRANSIENT_INFRA` | `RETRY_DEBIT` (soon, avoid peak), `WAIT` | Messaging the customer. Nothing is wrong on their side. |
| `INSUFFICIENT_FUNDS` | `WAIT` (to est. inflow), `SEND_PREDEBIT_NOTICE`+`RETRY_DEBIT`, `REQUEST_PTP`, `SEND_PAYMENT_LINK` | Immediate retry. Repeated messaging — it cannot create money. |
| `LIMIT_EXCEEDED` | `SPLIT_DEBIT`, `REREGISTER_MANDATE` (higher cap), `SEND_PAYMENT_LINK` | Retrying the same amount on the same mandate. |
| `AUTH_ARTEFACT` | `SEND_MESSAGE` (fix-it instructions), `REREGISTER_MANDATE`, `SEND_PAYMENT_LINK` | Silent retry — nothing changed. |
| `MANDATE_INVALID` | `REREGISTER_MANDATE(target_rail)`, `SEND_PAYMENT_LINK` | Any retry on the broken rail. |
| `MANDATE_REVOKED` | `SEND_MESSAGE` (re-consent), `SEND_PAYMENT_LINK`, `CLOSE` | Re-registering without fresh consent. |
| `ACCOUNT_TERMINAL` | `SEND_PAYMENT_LINK`, `ESCALATE_HUMAN`, `CLOSE(TERMINAL_RAIL)` | Anything on that rail. |
| `UNKNOWN` | `WAIT`, `SEND_PREDEBIT_NOTICE`+`RETRY_DEBIT` (once) | Escalation. You do not know enough yet. |

Two overlays applied after the matrix:

- If `hardship_score > config.hardship_threshold` (default 0.6), the eligible set is
  replaced by `{OFFER_ACCOMMODATION, ESCALATE_HUMAN, CLOSE(HARDSHIP)}`. RBI's draft
  recovery norms expect lenders to identify borrowers in repayment difficulty and offer
  guidance — an agent that knows who *not* to chase is the most differentiated thing in
  the build.
- If a `PromiseToPay` is `OPEN`, the eligible set is `{WAIT}` until
  `promised_date + grace_days`.

The `MANDATE_INVALID → REREGISTER_MANDATE(target_rail)` row is the demo's best moment:
an `AP17` NRE-account mandate that gets *repaired onto UPI Autopay* rather than retried.
Nobody's retry engine does this, because outside India there is nothing to arbitrage.
