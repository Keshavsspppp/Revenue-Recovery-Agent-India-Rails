# 03 — Simulator

Module: `app/sim/`

Every team building this will have a simulator, and most will build one whose customers
respond to messages because the author wanted them to. A judge who has worked in
payments spots that in one question. This spec is written to make that failure
structurally impossible.

## The one rule that makes it honest

> **A message can move `intent` and `annoyance`. A message can never move `balance`.**

A debit succeeds when the money is there. Messaging changes the probability that the
customer *pays through another route*, and it changes how annoyed they are. It does not
put money in the account. Every honest finding this project produces comes from that
asymmetry — it is why `WAIT` beats `SEND_MESSAGE` for the insufficient-funds class, and
why the holdout arm is not trivially beatable.

## Module boundary

`app/sim/` may import `app/domain/`. **Nothing in `app/plan/`, `app/diagnose/`,
`app/propose/` or `app/policy/` may import `app/sim/`.** Add:

```python
# tests/test_boundaries.py
def test_agent_cannot_see_simulator():
    for mod in ("app.plan", "app.diagnose", "app.propose", "app.policy"):
        assert "app.sim" not in _transitive_imports(mod)
```

Leaking the true salary date into the planner is the easiest way to accidentally fake
your result. This test is the guard.

---

## Latent account state

Never exposed to the agent. Generated once per account from the batch seed.

```python
@dataclass
class LatentAccount:
    inflow_day: int             # 1..28, salary day of month
    inflow_paise: int           # monthly credit
    balance_paise: int          # evolves daily
    burn_rate: float            # fraction of inflow spent per day, Gamma-distributed
    intent: float               # 0..1 — propensity to self-pay when prompted
    annoyance: float            # 0..1 — accumulates with contacts
    hardship: bool              # reduced/irregular inflow
    dispute_prone: bool
    mandate_defect: CauseClass | None   # a latent structural defect, if any
    responsiveness: float       # 0..1 — probability of answering voice / replying
```

### Generation (defaults in `config/default.yaml` → `sim:`)

```
inflow_day        ~ mixture: 0.55·Uniform{1..5}  (start of month)
                            0.25·Uniform{25..28} (month end)
                            0.20·Uniform{6..24}  (irregular / gig income)
inflow_paise      ~ LogNormal by merchant_category and city_tier
burn_rate         ~ Gamma(k=2.0, θ=0.018)      # ~3.6% of inflow/day mean
intent            ~ Beta(2.5, 2.0)
annoyance         = 0.0 at start
hardship          ~ Bernoulli(0.07)
dispute_prone     ~ Bernoulli(0.03)
responsiveness    ~ Beta(2.0, 3.0)
mandate_defect    ~ categorical, see §Defect mix
```

City tier shifts rail reliability, matching the wide metro/tier-3 gap reported in
Indian payment-success analyses. Keep the shift in config, not in code.

### Balance dynamics

Daily, in `LatentAccount.tick(day)`:

```
if day.day == inflow_day and not hardship:
    balance += inflow_paise
elif day.day == inflow_day and hardship:
    balance += inflow_paise * Uniform(0.3, 0.8)   # partial / delayed
    with prob 0.4: skip this month entirely

balance -= inflow_paise * burn_rate * Poisson_jitter
balance  = max(balance, 0)
```

This produces the sawtooth that makes `INSUFFICIENT_FUNDS` a *timing* problem: the
account is nearly empty just before payday and flush just after. The planner's job is
to infer that phase from observable history alone.

---

## Rail simulation

`app/sim/rails.py` implements the `RailAdapter` interface that a real integration would
also implement. This is the swap point.

```python
class RailAdapter(Protocol):
    def notify(self, mandate: Mandate, amount_paise: int,
               scheduled_for: datetime) -> NoticeReceipt: ...
    def attempt(self, mandate: Mandate, amount_paise: int,
                at: datetime, gate: GateDecision) -> AttemptResult: ...
    def mandate_status(self, mandate_id: str) -> MandateStatus: ...
    def register_mandate(self, account_id: str, rail: Rail,
                         cap_paise: int, gate: GateDecision) -> Mandate: ...
    def settlement_feed(self, since: datetime) -> list[Settlement]: ...
```

### Attempt resolution order

Order matters — it determines which code you observe when several things are wrong.
Real rails fail fast on structural checks before touching the account.

```python
def attempt(mandate, amount, at, gate) -> AttemptResult:
    assert gate.verdict is Verdict.ALLOW and gate.action_hash == action.hash()

    # 1. Notice window — rail-level rejection, never reaches the issuer
    if not notice_satisfied(mandate, at, rail.notice_hours):
        return AttemptResult(False, "NOTICE_WINDOW_VIOLATION", None, 0)

    # 2. Mandate structure
    if mandate.status is REVOKED:   return fail("AP53")   # MANDATE_REVOKED
    if mandate.status is INVALID:   return fail(code_for(mandate.defect))
    if mandate.status is PENDING_AFA: return fail("AP39") # AUTH_ARTEFACT

    # 3. Limits
    if amount > applicable_cap(mandate, merchant_category):
        return fail("AP58")                                # LIMIT_EXCEEDED
    if amount > mandate.cap_paise:
        return fail("AP58")

    # 4. Infrastructure — time-of-day dependent
    if rng.random() < p_infra(at, city_tier):
        return fail("AP66")                                # TRANSIENT_INFRA

    # 5. Funds — the only check that consults latent balance
    if latent.balance_paise < amount:
        return fail("AP01")                                # INSUFFICIENT_FUNDS

    latent.balance_paise -= amount
    return AttemptResult(True, "SUCCESS", at + timedelta(seconds=27), rail.attempt_fee_paise)
```

### `p_infra(at, city_tier)`

```
base       = {1: 0.012, 2: 0.020, 3: 0.030}[city_tier]
peak_mult  = 2.2 if 19:00 <= at.hour < 22:00 else 1.0   # evening load
weekend    = 1.15 if at.weekday() >= 5 else 1.0
p_infra    = min(base * peak_mult * weekend, 0.12)
```

Grounded in two published observations: NPCI's stated technical-decline target is under
1% system-wide (merchant-side blended success typically 92–96%), and vendor analyses
report success dropping meaningfully during the 19:00–22:00 peak. Tier-3 sits materially
below metro. Treat the exact numbers as **declared assumptions** and cite the sources in
`12-GLOSSARY.md`.

### Defect mix (first-failure cause distribution)

Calibration target for a fresh batch. State it in the report.

| CauseClass | Share | Notes |
|---|---|---|
| `INSUFFICIENT_FUNDS` | 0.42 | the timing bucket |
| `TRANSIENT_INFRA` | 0.24 | retryable immediately |
| `AUTH_ARTEFACT` | 0.11 | OTP / registration artefacts |
| `MANDATE_INVALID` | 0.08 | NRE, minor, wrong account type |
| `LIMIT_EXCEEDED` | 0.06 | above AFA-free cap |
| `MANDATE_REVOKED` | 0.05 | customer cancelled |
| `ACCOUNT_TERMINAL` | 0.04 | closed / frozen |

---

## Self-cure — the thing that makes the holdout non-trivial

Self-cure is **not** a bolt-on hazard. It emerges from two mechanisms, both of which
operate identically in both arms:

1. **The merchant default policy.** The holdout arm still gets the merchant's normal
   behaviour: a pre-debit notice and a retry on day +3 and day +7. When the salary lands
   on day +5, the day +7 retry succeeds — with no agent involved. This is the bulk of
   self-cure and it is exactly what the treatment arm must beat.

2. **Customer-initiated payment.** A daily hazard:

```
λ_selfpay(day) = base_selfpay
               × (1 + 2.0 · intent)
               × (1 + 1.5 · notified_recently)     # the mandatory notice informs them
               × funds_available(day)              # 1 if balance ≥ amount else 0.15
               × (1 − 0.6 · annoyance)             # annoyed customers disengage
base_selfpay = 0.015
```

Note that the pre-debit notice — which is *legally mandatory* — is itself an
intervention that lifts self-pay. That is a real and slightly awkward finding: some of
what a naive system credits to its clever messaging is actually caused by the
notification the regulator forced it to send. Report it.

### Acceptance target

> On a fresh batch with the **do-nothing** policy (notices + default retries only),
> holdout recovery within a 30-day horizon must land in **0.30–0.50** of at-risk value.

If it is near zero, the simulator is lying and every downstream number is inflated.
`tests/test_selfcure.py` asserts this range and is a **stop-the-line** test.

---

## Contact effects

```python
def apply_contact(latent, action, channel):
    latent.annoyance = min(1.0, latent.annoyance + ANNOY[channel])
    if action in (SEND_MESSAGE, SEND_PAYMENT_LINK, REQUEST_PTP, VOICE_CONFIRM_PTP):
        latent.intent = min(1.0, latent.intent + INTENT_LIFT[channel]
                                 * (1 - latent.intent)      # diminishing returns
                                 * (1 - latent.annoyance))  # annoyed ⇒ no lift
    # balance is untouched. Always.
```

```yaml
annoy:        { SMS: 0.06, WHATSAPP: 0.08, EMAIL: 0.02, PUSH: 0.03, VOICE: 0.22 }
intent_lift:  { SMS: 0.10, WHATSAPP: 0.16, EMAIL: 0.05, PUSH: 0.06, VOICE: 0.30 }
```

### Opt-out, complaint, dispute hazards

```
P(opt_out   | contact) = 0.004 + 0.09 · annoyance²
P(complaint | contact) = 0.001 + 0.03 · annoyance²
P(dispute   | contact) = 0.02 · dispute_prone + 0.01 · annoyance
```

The quadratic term is what punishes hammering: three contacts is cheap, eight is
expensive. This is what makes the λ frontier in `08-EVALUATION.md` bend.

### Hardship signals (observable)

The agent must be able to *detect* hardship without seeing `latent.hardship`. Emit these
observable proxies:

- repeated `AP01` across ≥2 consecutive cycles,
- multiple mandates failing in the same window (simulate a second merchant's debits),
- explicit distress language in a simulated reply (`REQUEST_PTP` responses draw from a
  small phrase bank, ~12% distress for hardship accounts),
- a broken PTP followed by another `AP01`.

`hardship_score` in `AgentState` is computed from those signals only. Report the
detector's precision/recall against `latent.hardship` in the evaluation — that is a
legitimate, interesting number and it costs you nothing to produce.

---

## Voice response model

Only reached via `VOICE_CONFIRM_PTP`, and only for accounts already at PTP stage.

```
P(answer)        = 0.35 · responsiveness · (1 − 0.5·annoyance) · in_window(08:00–19:00)
given answered:
  P(PTP_CAPTURED)= 0.55 · intent
  P(DISPUTE)     = 0.10 · dispute_prone + 0.03
  P(HARDSHIP)    = 0.40 if latent.hardship else 0.02
  P(CALLBACK)    = remainder
else: NO_ANSWER
```

Distress or dispute language ends the call and routes to a human. That is a rule in the
policy engine, not a model output.

---

## Batch generation

```
rr simulate --accounts 2000 --seed 42 --horizon 30 --out data/batch_001.db
```

Produces, per account: one `Account`, 1–2 `Mandate`s, one `BillingCycle` with a failed
first attempt, and a `DETECT` event. Latent state is stored in a **separate table
`latent_truth`** in the same DB, used only by the simulator and the evaluator's
hardship-detector scoring. Add a test that no module under `app/plan|diagnose|propose`
issues a query against it.

## Determinism

Same seed + same config ⇒ identical ledger except `wall_clock_at`. One
`numpy.random.Generator` per batch, split into named sub-streams
(`rng.spawn()`) for accounts, rails, and hazards, so adding a new hazard does not
reshuffle account generation and invalidate a previously reported run.
