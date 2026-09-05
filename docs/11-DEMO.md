# 11 — Demo

Six minutes. The claim comes first because it is the claim; everything after it is
evidence, in the order a sceptic would ask for it.

**Run the batch before you present.** Nothing in this script depends on a live LLM call, a
network, or a model behaving. Figures below are batch `demo`, seed 42, 3,000 accounts,
₹1,49,73,344 at risk — regenerate with the commands in §Preflight and they will match.

---

## Preflight

```bash
rr simulate --accounts 3000 --seed 42 --out data/demo.db
rr run --batch data/demo.db --policy agent --holdout 0.2
rr report --batch data/demo.db     # must print: notice window violations 0
rr verify --batch data/demo.db     # must end: ok=True
uvicorn app.api.main:app --host 127.0.0.1 --port 8010
```

Then, before anyone is in the room:

1. Open the Razorpay dashboard in a second tab, **Test Mode → Payment Links**. Have one
   `plink_…` id ready to paste. `plink_TY8KntDhhZ0g81` (₹40,435) is the useful one — it is
   over the ₹15,000 ceiling, which is the population the split-debit story is about.
2. Pick your account in the trail selector. It groups by what happened; choose one from
   *mandate repaired onto another rail* or *stopped: not worth continuing* and know its
   story. Hunting live costs a minute you do not have.
3. Load the page once and let the scoreboard finish. Cold it is about four seconds.

---

## 0:00 — Lead with the number you did not earn (60s)

Open on the scoreboard, already computed. Say the holdout figure before anyone asks for
it: that one move tells the room you know where the trap is, and everything after lands
differently.

> "Three thousand at-risk accounts on Indian rails, twenty percent randomised holdout. The
> agent recovered ₹63.3 lakh. **The holdout recovered ₹45.9 lakh on its own** — that is
> self-cure, and if I counted it as mine my headline would be 52% instead of 14%. The real
> number is ₹17.4 lakh incremental, 95% interval ₹7.3 lakh to ₹26.7 lakh. I ran four
> seeds: positive on all four, clear of zero on three."

*On screen:* the claim — agent bar, holdout bar, and the interval drawn on its axis with
zero marked.

---

## 1:00 — Why this is not Stripe's problem (60s)

No chart. This is the beat that says you understand the domain rather than the ML.

> "Outside India this is retry-timing, and Stripe solved it with a 500-feature ensemble. We
> were not going to beat that in a weekend, so we did not try.
>
> On Indian rails RBI's e-mandate framework requires a pre-debit notice at least 24 hours
> before each debit — on our reading, before each *retry*. That turns an attempt into a
> pre-announced, budgeted event instead of a free loop. You get a handful of shots per
> cycle and a wasted one costs a customer contact."

---

## 2:00 — Where the value is, and where it is negative (75s)

Scroll to the per-cause cut. Point at the last row first.

> "Insufficient funds is +20 points and transient infrastructure is +25 — those are timing
> problems, and waiting for the salary date solves them. **Account terminal is minus 15.6
> points.** On closed and blocked accounts the agent is worse than doing nothing, and that
> row is on the screen because taking it off would make the headline a lie."

*On screen:* by cause — seven rows, one of them red.

---

## 3:15 — The gate refuses you, live (60s)

Set the clock to 19:30, leave the action on `VOICE_CONFIRM_PTP`, press Evaluate. Then
change the time to 14:00 and press it again.

> "That is not a prompt asking the model nicely. It is a pure function of 29 rules that
> every action passes through before it reaches a rail, and the rail adapter refuses
> anything without a matching signed decision. **The refusal you just watched is now in the
> ledger** — written as a dry run, and you will see it in the trail in thirty seconds."

*On screen:* DENY, `POL-QH-001`, and the RBI basis underneath it.

---

## 4:15 — The trail, and the harm number beside the money (50s)

Show one account end to end, then point at the harm table.

> "Detect, assign, diagnose, narrow, propose, gate, execute, observe, close — every row is
> a ledger entry, the chain is hashed, and `rr verify` checks it. And the agent opts out
> 6.7 customers per thousand against the merchant baseline's 16.6. **A recovery number
> without its harm number is an incomplete result**, so they are on the same screen."

---

## 5:05 — The half that is real (55s)

Scroll to the Razorpay panel. Copy a `plink_…` id, paste it into the dashboard tab.

> "The measurement is simulated, because you cannot randomise a control group against a
> live provider and you cannot authorise two thousand mandates in a browser. **These are
> not simulated.** Same agent, same gate, same ledger, real Razorpay test-mode objects —
> that id is in your dashboard and that link opens."

If you have paid one with a test card beforehand, press **Check payments**: the row turns
green and the payment is written to the ledger as a confirmed recovery, counted from what
Razorpay says rather than from the fact that we asked.

---

## The one thing to volunteer

Say this before a judge finds it, or the honesty story collapses.

**One action carries the entire lift.** Disabling `SPLIT_DEBIT` and re-running the
identical batch drops the result to ₹1,31,826 with an interval of [−₹8,32,618,
+₹10,62,907] — which contains zero.

Why the population is big: 358 of 3,000 accounts (11.9%) sit above their binding
per-transaction ceiling — 185 above RBI's ₹15,000 AFA-free cap, 186 above their own
registered mandate cap. Their mean amount is ₹15,667 against a batch mean of ₹4,991.
Before the split existed those accounts had *no debit action at all*: a retry was stripped
as impossible and nothing replaced it. The agent was sitting out the heavy tail of its own
batch.

> "The lift is concentrated in one mechanism, and I can show you the A/B. The mechanism is
> a real RBI constraint, not a tuning knob: two presentations of ₹10,000 clear a ₹15,000
> ceiling that one of ₹20,000 does not."

### Replication

| seed | agent | holdout | incremental | 95% CI | excludes zero |
|---|---|---|---|---|---|
| 42 | 52.3% | 38.0% | ₹17,39,386 | ₹7.3L – ₹26.7L | yes |
| 7 | 54.7% | 41.9% | ₹15,18,583 | ₹5.6L – ₹24.9L | yes |
| 99 | 50.3% | 44.3% | ₹7,24,841 | −₹4.0L – ₹17.4L | **no** |
| 2024 | 55.2% | 39.3% | ₹19,45,379 | ₹9.5L – ₹28.6L | yes |

> "Positive on every seed I ran, clear of zero on three of four. Seed 99 does not — the
> holdout self-cured at 44.3% there. The direction is stable; the magnitude moves by about
> 2.7×. At three thousand accounts one run in four is underpowered, and I would want ten
> thousand before quoting a point estimate."

---

## If they push on a number

| they ask | you say |
|---|---|
| "What's the recovery rate?" | All three, with the denominator. 66.1% per retried (n=1,010), 55.5% per addressable (n=2,123), 52.3% per all failed (n=2,399). Vendors quote the first. The only number I claim is the *gap*. |
| "How do I know the holdout is clean?" | Every account has its own RNG stream, so the control arm cannot depend on what the treatment arm did. When the agent changed, the holdout came back identical to the paisa — ₹45,88,019, 38.0%. |
| "Is it just recovering faster?" | Slower. Median 13.3 days against 3.0. The baseline retries on a calendar and catches easy self-cures early; the agent waits for the inflow and retries once, at a much higher hit rate. |
| "What did it cost the customer?" | Opt-outs 6.7 per 1,000 against 16.6, complaints 0.0 against 1.7, cancellations 0.8 against 3.3. Worse on two: disputes 2.5 against 0.0, and hardship exits 10.0 against 0.0 — that second is the agent *choosing* to route people out. |
| "Where's the LLM?" | Measured and not shipped in the money path. On 185 decisions it answered, it agreed with the planner 95.7% of the time and changed 8, all WAIT into a link or a message. Money difference ₹0. |
| "Is the compliance real or a prompt?" | 29 versioned rules in `app/policy/rules.py`, each with the regulation it implements. `rr rules` prints the catalogue and its hash. Zero notice-window violations across 3,000 accounts. |

---

## If something breaks

- **The page sits on skeletons.** The API is not running, or is on another port. Each panel
  shows its own error with the command to start it.
- **A scoreboard says "batch not run".** That batch was simulated but never run. The error
  names the command.
- **`rr live` says nothing was created.** Accounts already live are skipped on purpose —
  the slice is idempotent so a rehearsal does not duplicate customer-visible objects.
- **Groq is down.** Irrelevant. The run you already have is unaffected; the deterministic
  proposer is the control arm, not a degraded mode.
