# 08 — Evaluation

Module: `app/eval/`

This is the deliverable. The agent is the easy part; this is what clears the bar.

Build the evaluator **before** the agent and run it against the do-nothing policy. That
first number is your floor, and having it early stops you from ever reporting a lift you
cannot locate.

---

## Arms

Randomisation happens **at the failure event**, before anything else touches the account,
and is written as the `ASSIGN` ledger event (`02-LEDGER.md` invariant 3).

```python
def assign_arm(account_id: str, batch: Batch) -> Arm:
    # stratified on (cause_class_L1, amount_band, merchant_category)
    # deterministic given batch seed → reproducible arms
```

- Default `holdout_frac = 0.20`. On 2,000 accounts that is 400 holdout / 1,600 treatment —
  enough to detect a ~5pp difference at conventional power.
- **Stratify.** Simple randomisation on 2,000 accounts can imbalance the terminal cause
  classes badly enough to swamp the effect you are measuring.
- The holdout arm receives the **merchant default policy** (`nothing`): pre-debit notice
  plus retries on day +3 and +7. Not "nothing at all" — that would be a straw man, and
  the pre-debit notice is legally mandatory anyway.
- Report a **balance table** (arm × cause class × amount band) so imbalance is visible
  rather than assumed away.

---

## The three denominators

"Recovery rate" is a fraction and the denominator is a choice. There are three in
circulation, and vendors quote the flattering one. **Publish all three, with the
denominator printed next to each.** This single act of disclosure does more for
credibility than any model improvement, and it pre-empts the first question a
knowledgeable judge asks.

| Metric | Denominator | Character |
|---|---|---|
| `rate_per_retried` | accounts the agent actually attempted | Largest. Hides low coverage. |
| `rate_per_addressable` | failures excluding `ACCOUNT_TERMINAL` and `MANDATE_REVOKED` | Middle. Every vendor defines "addressable" differently — so define yours in one sentence on the slide. |
| `rate_per_all_failed` | all failed volume entering the batch | Smallest. The honest one. |

---

## Metric definitions

All money in paise; format at the edge. `T` = treatment, `H` = holdout.

```
at_risk_value          = Σ cycle.amount_paise over all accounts in the batch
gross_recovered_T      = Σ amount_paise where settled=1, arm=treatment
recovery_rate_T        = gross_recovered_T / at_risk_value_T
recovery_rate_H        = gross_recovered_H / at_risk_value_H

incremental_rate       = recovery_rate_T − recovery_rate_H
incremental_recovered  = incremental_rate × at_risk_value_T        ← THE HEADLINE

cost_of_recovery_T     = Σ action costs incurred in the treatment arm
                         (attempt fees + message costs + voice minutes
                          + human escalation minutes at a stated rate)
net_incremental        = incremental_recovered − (cost_T − cost_H)

contacts_per_recovery  = contacts_T / recovered_accounts_T
days_to_recover_p50/p90= percentiles of (settled_at − detected_at), by arm
```

`cost_H` is subtracted because the holdout arm also spends money (notices, default
retries). The comparison is between two *policies*, not between a policy and free.

### Harm counters — same screen as the money

Per thousand accounts, both arms:

```
opt_outs_per_1k, complaints_per_1k, mandate_cancellations_per_1k,
disputes_per_1k, hardship_exits_per_1k, contacts_per_account_p50/p95
```

Put these on the **same screen** as the recovery figures, not on a separate ethics
slide. A recovery number without its harm number is an incomplete result, and presenting
them together is the clearest signal that you understood the brief's word "compliant."

### Compliance counters

```
policy_denials_by_rule_id       # proof the gate is live, not decorative
notice_window_violations        # MUST be 0; non-zero means a scheduler bug
unmapped_code_count             # taxonomy drift
circuit_breaker_trips
```

`notice_window_violations` is a **defect counter for your own system**, not a customer
failure. Failing loudly on your own compliance mistake is more convincing than never
appearing to make one.

---

## Confidence intervals

A point estimate of lift on 2,000 accounts without an interval is not a result.

**Rate difference** (two-proportion normal approximation):

```
SE   = sqrt( p_T(1−p_T)/n_T + p_H(1−p_H)/n_H )
CI95 = (p_T − p_H) ± 1.96 · SE
```

**Value-weighted lift** — amounts are heavy-tailed (a ₹42,000 EMI and a ₹299
subscription in the same batch), so the normal approximation is unsafe. Use a
**stratified bootstrap over accounts**, 10,000 resamples, resampling within
(arm × cause class × amount band):

```python
def bootstrap_incremental(events, n=10_000, seed=7) -> tuple[float, float, float]:
    """returns (point, lo95, hi95) of incremental_recovered_paise"""
```

**Report the interval even when it straddles zero.** "Incremental recovery ₹1.42L,
95% CI [−₹0.11L, ₹2.9L]" is an honest, publishable result on 2,000 accounts. Claiming
significance you do not have is the fastest way to lose a knowledgeable room.

If the interval is uncomfortably wide, the fix is more accounts (the simulator is free) —
not a narrower definition of success.

---

## Segment cuts

Every metric above, broken down by:

- **cause class** — where the real insight lives; the agent will crush some classes and
  do nothing for others, and saying which is the interesting finding;
- **amount band** — the economic stopping rule should show up as visibly lower effort on
  small tickets;
- **merchant category** — high AFA-free-cap categories behave differently;
- **city tier** — infra failures concentrate in tier 3;
- **inflow confidence bucket** — the planner should beat baseline most where the inflow
  estimate is confident. If it does not, the estimator is not doing what you claim.

That last cut is a genuine mechanism check: it tests the causal story, not just the outcome.

---

## The λ frontier

Re-solve the planner at `λ ∈ {0, 0.25, 0.5, 1.0, 2.0}` and plot **net incremental
recovery against opt-outs per 1,000**. Value iteration takes seconds, so this is cheap.

The sentence you want to be able to say is:

> "At λ=0 we recover ₹X with 24 opt-outs per thousand. At λ=1 we recover ₹X−4% with 11.
> Here is the dial, and here is why we shipped it at 0.5."

That demonstrates harm was **priced**, not mentioned. It is the single most persuasive
artefact in the whole evaluation, and almost nobody produces it.

---

## Diagnostics (report, do not headline)

| Diagnostic | Why it earns its place |
|---|---|
| `agent / oracle` ratio | Share of achievable value captured (`06-PLANNER.md`). Tells the room how much headroom remains. |
| Hardship detector precision/recall | Scored against `latent_truth`. Free to compute, directly relevant to conduct. |
| Cause posterior accuracy | Sanity check only — `action_hit_rate` is the metric that matters. |
| Counterfactual regret by cause | Replay the seeded world under the best-in-hindsight action. |
| Self-cure share | `recovery_rate_H / recovery_rate_T` — say this number out loud before anyone asks. |
| Notice-attributable lift | Recovery lift from the *mandatory* pre-debit notice alone. Some of what a naive system credits to clever messaging is caused by the notification the regulator forced it to send. Report it. |

---

## Report format

`rr report --batch data/batch_001.db` prints this, and `GET /batches/{id}/scoreboard`
returns the same as JSON.

```
BATCH bat_001   seed=42   policy=pol_2026.09.1   λ=0.5   proposer=groq:openai/gpt-oss-120b
accounts 2000 (treatment 1600 / holdout 400)   horizon 30d   at-risk ₹41,20,300

  RECOVERY
    gross recovered (treatment)          ₹22,84,100    55.4%
    holdout recovered (rate-adjusted)    ₹17,05,900    41.4%   ← self-cure baseline
    INCREMENTAL RECOVERED                ₹ 5,78,200    14.0%   95% CI [₹3.9L, ₹7.6L]
    cost of recovery (T − H)             ₹   41,700
    NET INCREMENTAL                      ₹ 5,36,500

  RATES (denominator printed)
    per retried attempts                 62.1%   (n=1,412 attempted)
    per addressable failures             55.4%   (excl. terminal + revoked)
    per all failed volume                48.7%   (n=1,600)

  EFFICIENCY
    contacts per recovery                 1.8
    days to recover                       p50 6.2   p90 17.4
    agent / oracle                        0.71

  HARM (per 1,000 accounts)          treatment   holdout
    opt-outs                              14.4       6.2
    complaints                             3.1       1.9
    mandate cancellations                  5.0       4.4
    hardship exits                        11.9       0.0
    contacts per account (p95)             3.0       2.0

  COMPLIANCE
    policy denials      POL-QH-001 214 · POL-FREQ-001 96 · POL-PTP-001 51 · POL-STOP-001 12
    notice window violations               0
    unmapped rail codes                    3
    circuit breaker                        not tripped

  TERMINAL STATES
    RECOVERED 887 · CYCLE_ENDED 402 · EV_BELOW_THRESHOLD 168 · TERMINAL_RAIL 71
    HARDSHIP 19 · OPTED_OUT 23 · DISPUTED 14 · FATIGUE_EXHAUSTED 16
```

*(Figures above are an illustrative layout, not results. Your run produces its own.)*

---

## Tests

- `test_holdout_never_treated` — no `EXECUTE` in the holdout arm outside the default policy.
- `test_arm_balance` — chi-square on the stratification table; fails on material imbalance.
- `test_metrics_sum` — per-segment recovered values sum to the total.
- `test_bootstrap_stable` — two seeds give overlapping intervals.
- `test_report_matches_ledger` — every reported figure is recomputable from the raw
  events by an independent query. The report is a **view**, never a separate accounting.
