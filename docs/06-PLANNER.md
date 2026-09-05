# 06 — Planner

Module: `app/plan/`

This is the algorithmic content of the project. Everything else is plumbing that makes
the number believable; this is the part that makes the number bigger.

## The reframe

Because every retry is pre-announced 24 hours ahead and consumes a customer contact, the
question is **not** "which day maximises P(success)". It is:

> Given ~4 attempt slots, ~3 contact slots and a 30-day horizon, which sequence of
> actions maximises expected recovered value minus cost minus priced harm — including
> the option to spend nothing and wait?

That is a finite-horizon Markov decision process with budgets. It is small enough to
solve exactly by backward induction, which is both better and easier to explain than
fitting a model.

---

## State

Discretised so the state space stays enumerable.

```python
@dataclass(frozen=True)
class PlanState:
    days_left: int              # 0..30
    attempts_left: int          # 0..4
    contacts_left: int          # 0..3
    voice_left: int             # 0..1
    notice_pending: bool        # a notice is issued; an attempt is unlocked tomorrow
    cause: CauseClass           # argmax of the posterior (see §Posterior handling)
    inflow_bucket: int          # 0..4, days until estimated inflow: 0, 1-2, 3-5, 6-10, >10
    mandate_ok: bool            # at least one ACTIVE mandate on some rail
    alt_rail_available: bool    # a different rail could be registered
    ptp_open: bool
    fatigue_bucket: int         # 0..2, from contacts already made
```

Size: `31 × 5 × 4 × 2 × 2 × 8 × 5 × 2 × 2 × 2 × 3 ≈ 1.2M` states — well within reach of a
memoised backward pass in numpy, and it solves in seconds. If it feels large, collapse
`days_left` to weekly buckets past day 14.

---

## Inflow phase estimation

The planner's most valuable single input, and it must come from **observable history
only** — the timestamps of the account's past *successful* debits. Never the simulator's
salary date. (`tests/test_boundaries.py` enforces this.)

```python
@dataclass(frozen=True)
class InflowEstimate:
    day_of_month: int | None    # circular mean of successful-debit days
    concentration: float        # 0..1, from circular variance — how regular they are
    n_observations: int
```

Use a circular mean, because day 30 and day 1 are two days apart, not twenty-nine:

```
θ_i   = 2π · day_i / 30
x̄, ȳ = mean(cos θ), mean(sin θ)
day   = round(30 · atan2(ȳ, x̄) / 2π) mod 30
R     = sqrt(x̄² + ȳ²)            # concentration, 0..1
```

With `n_observations < 2`, fall back to the population prior (start-of-month heavy, per
`03-SIMULATOR.md`) and set `concentration = 0.2`. Report how often the fallback fires —
cold-start accounts are a real weakness and naming it is better than hiding it.

---

## Success model

`P(success | RETRY_DEBIT at day d)` factorises into three independent terms. Keep it
factorised: each factor is separately explainable to a judge, which a single fitted
model is not.

```
P_success(d) = P_funds(d) · P_infra(d) · P_mandate
```

**Funds** — logistic in days relative to estimated inflow, scaled by concentration:

```
Δ        = signed_circular_days(d, inflow.day_of_month)   # negative = before inflow
P_funds  = σ( a + b·Δ_clipped + c·concentration )
           with Δ_clipped = clip(Δ, -10, +10)
defaults : a = -0.4, b = 0.45, c = 1.1
```

The shape is what matters: probability rises sharply just after the estimated inflow
day and decays as the month burns down. Fit `a, b, c` by logistic regression on the
*training* batch's attempt outcomes (`04-CAUSE-TAXONOMY.md` §Model, same train/eval
split discipline), or ship the defaults and say they are priors.

**Infra** — the mirror of the simulator's `p_infra`, but estimated from observed
attempt outcomes by hour bucket, not read from config. Avoid 19:00–22:00.

**Mandate** — `1.0` if an `ACTIVE` mandate exists on the target rail with a sufficient
cap, else `0.0`. This is what makes `REREGISTER_MANDATE` mechanically necessary rather
than a nice idea.

For non-debit actions:

```
P(recover | SEND_MESSAGE)       = λ_selfpay_lift(channel) · P_funds(d..d+3) · intent_proxy
P(recover | SEND_PAYMENT_LINK)  = higher lift, no mandate dependency, no notice needed
P(recover | REREGISTER_MANDATE) = P(customer completes AFA) · future P_success
P(recover | REQUEST_PTP)        = P(PTP captured) · P(PTP kept | confidence)
P(recover | WAIT)               = 0 immediately, but preserves budget → value in V(s')
```

`intent_proxy` must be observable: prior responses to messages, prior self-pays, channel
engagement. Not `latent.intent`.

---

## Objective and backward induction

```
V(s) = max over eligible a of Q(s, a)

Q(s, a) = p(a,s) · amount_paise
        − cost(a)
        − λ · harm(a) · amount_scale
        + (1 − p(a,s)) · V(s′(a))

V(s) = 0 when days_left == 0
```

- `amount_scale` normalises harm against money so λ is interpretable. Use
  `amount_scale = median_cycle_amount_paise` for the batch, and state it.
- `λ` (`config.lambda_harm`) is the single dial for the harm/recovery trade-off. Default
  `0.5`. The λ frontier in `08-EVALUATION.md` is produced by re-solving at
  `λ ∈ {0, 0.25, 0.5, 1.0, 2.0}` — cheap, because solving is seconds.
- **Stopping falls out**: if `max_a Q(s,a) < 0`, the chosen action is
  `CLOSE(EV_BELOW_THRESHOLD)`. That is a *result*, not a constant. It naturally stops
  chasing a ₹299 subscription after two attempts and keeps working a ₹42,000 EMI.

### Notice coupling — the constraint that defines the problem

`RETRY_DEBIT` is not directly schedulable. It requires `notice_pending == True`, which
only `SEND_PREDEBIT_NOTICE` sets, and which becomes actionable one day later.

```
SEND_PREDEBIT_NOTICE at day d  →  s′ has notice_pending = True, day d+1
RETRY_DEBIT at day d requires    s.notice_pending == True
                               →  s′ has notice_pending = False, attempts_left − 1
```

So committing to an attempt means **committing a day in advance, before you know
whether the money arrived.** That is the interesting decision in the whole system, and
it is worth putting on a slide: the agent is forecasting the customer's balance a day
ahead in order to buy an option on a debit.

### Posterior handling

The MDP indexes on a single `cause`. Two options:

1. **Cheap and honest (default):** solve per cause class, then take the posterior-weighted
   argmax over actions:
   `a* = argmax_a Σ_c P(c) · Q_c(s, a)`.
2. **Expensive:** add a belief dimension. Not worth it at this scale.

Use option 1 and say why: it keeps `Q` inspectable per cause, which is what you show in
the audit trail.

---

## Baseline policies

You must ship all three. The margin over the baselines is a more credible claim than a
raw recovery rate.

| Policy | Behaviour | Role |
|---|---|---|
| `nothing` | Merchant default only: notice + retry on day +3 and +7 | The floor. Also the **holdout arm's** behaviour. |
| `fixed` | Notice + retry on days +1, +3, +7, +14; one SMS on day +2 | The realistic incumbent. Beat this. |
| `agent` | The planner above | Yours |
| `oracle` | Same planner but given `latent.balance` | The **ceiling**. Shows headroom. |

`oracle` is a diagnostic, never a reported result. If `agent` is close to `oracle`, your
inflow estimator is good and the remaining gap is irreducible. If it is far below,
improve estimation rather than adding actions. Reporting the ratio `agent/oracle` is a
genuinely strong, unusual result — it tells the room how much of the achievable value
you captured.

---

## Runner loop

```python
for day in range(cycle.horizon_days):
    clock.advance_to(day)
    for account in batch.open_accounts():
        if account.arm is Arm.HOLDOUT:
            action = merchant_default(account, day)          # no planner, ever
        else:
            state    = build_agent_state(account, clock)      # observable only
            posterior= diagnose(state)                        # 04
            eligible = eligible_actions(posterior, state)     # 04 §matrix
            proposed = proposer.propose(state, posterior, eligible)  # 07
            action   = proposed if proposed in eligible else planner.best(state, eligible)
        gate = policy.evaluate(action, ctx)
        ledger.append(gate_event(gate))
        if gate.verdict is Verdict.ALLOW:
            result = executor.run(action, gate)
            ledger.append(execute_event(action, result))
        observe(account, clock)                               # settlement feed, PTP resolution
    check_circuit_breaker(batch)
```

Note the fallthrough: the LLM proposes, and if its proposal is not in the eligible set
the **planner's** choice is used, not a retry of the LLM. See `07-PROPOSER-GROQ.md`.

---

## Tests

- `test_planner_waits` — `INSUFFICIENT_FUNDS`, estimated inflow 3 days out, attempts
  available ⇒ chooses `WAIT` (or a notice timed to land after inflow), never an
  immediate `RETRY_DEBIT`.
- `test_planner_repairs` — `MANDATE_INVALID` with `alt_rail_available` ⇒
  `REREGISTER_MANDATE`, never `RETRY_DEBIT`.
- `test_stopping_is_economic` — a ₹99 cycle with 1 attempt left and low `P_funds`
  closes as `EV_BELOW_THRESHOLD`; the same state with a ₹42,000 cycle does not.
- `test_notice_coupling` — `RETRY_DEBIT` is never selected from a state with
  `notice_pending == False`.
- `test_lambda_monotone` — as λ rises, total contacts fall monotonically. If it does not,
  harm is not actually priced and the frontier plot will be nonsense.
- `test_value_iteration_converges` — `V` is finite and stable for all reachable states.
