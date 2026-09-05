# Revenue Recovery Agent — India Rails

An agent that detects failed recurring payments on Indian payment rails, diagnoses why each
one failed, chooses a bounded intervention under a hard compliance gate, and — the part
that matters — **proves how much of the recovered money it actually caused.**

Built for the Razorpay Buildathon, Track 03: AI Revenue Recovery.

<sub>Python 3.11+ · FastAPI · SQLite (WAL) · numpy · React + Vite + TypeScript · 339 tests</sub>

---

## The result

3,000 at-risk accounts, seed 42, ₹1,49,73,344 at risk, against a randomised 20% holdout
running the merchant's normal behaviour.

| | recovered | rate |
|---|---:|---:|
| agent | ₹63,27,406 | 52.3% |
| **holdout — would have come back anyway** | **₹45,88,019** | **38.0%** |
| **incremental** | **₹17,39,386** | **14.4%** |

95% CI `[₹7,60,844, ₹26,46,298]`, stratified bootstrap over accounts. Opt-outs 6.7 per
1,000 against the merchant baseline's 16.6. **Zero notice-window violations.**

**The headline is the gap, not the gross.** 72% of what the agent "recovered" is self-cure
— customers who would have paid regardless. Reporting the gross figure is the single most
common way this problem is misreported, and a randomised control arm is the only way to
tell the two apart.

<details>
<summary><b>Replication across four seeds</b> — positive on all four, clear of zero on three</summary>

| seed | agent | holdout | incremental | 95% CI | excludes zero |
|---|---:|---:|---:|---|---|
| 42 | 52.3% | 38.0% | ₹17,39,386 | ₹7.6L – ₹26.5L | yes |
| 7 | 54.7% | 41.9% | ₹15,18,583 | ₹5.6L – ₹24.9L | yes |
| 99 | 50.3% | 44.3% | ₹7,24,841 | −₹4.0L – ₹17.4L | **no** |
| 2024 | 55.2% | 39.3% | ₹19,45,379 | ₹9.5L – ₹28.6L | yes |

The direction is stable; the magnitude moves by about 2.7×. At 3,000 accounts one run in
four is underpowered, so quote the range rather than a point estimate.
</details>

---

## Why Indian rails are a different problem

Outside India, failed recurring payments are a retry-*timing* problem, and Stripe solved it
with a 500-feature ensemble. That framing does not transfer.

RBI's consolidated e-mandate framework requires a **pre-debit notification at least 24
hours before each debit** — on our reading, before each *retry*. That turns an attempt from
a free loop into a pre-announced, budgeted, customer-visible event. You get a handful of
shots per cycle, and a wasted one costs a customer contact.

So the problem is not "predict the best retry day". It is **allocating a tiny budget of
expensive actions** — attempt, split the attempt under the ₹15,000 additional-factor
ceiling, notify, message, repair the mandate onto another rail, capture a promise to pay,
or deliberately wait for the customer's salary date — under hard compliance constraints,
with a stopping rule that has an economic basis rather than a hardcoded attempt count.

---

## Quickstart

```bash
git clone https://github.com/Keshavsspppp/Revenue-Recovery-Agent-India-Rails.git
cd Revenue-Recovery-Agent-India-Rails
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[api,dev]"

rr simulate --accounts 3000 --seed 42 --out data/demo.db
rr run --batch data/demo.db --policy agent --holdout 0.2
rr report --batch data/demo.db        # the scoreboard
rr verify --batch data/demo.db        # hash chain + 8 ledger invariants → ok=True
```

Then the dashboard:

```bash
cd web && npm install && npm run build && cd ..
uvicorn app.api.main:app --port 8010          # http://127.0.0.1:8010 · API docs at /docs
```

> One policy per batch. The world is regenerated from the seed and the ledger is
> append-only, so a second run cannot be reconciled with the first. To compare against a
> baseline, simulate to a fresh path with the same seed and run `--policy nothing`.

**No API keys are required.** Without `GROQ_API_KEY` the deterministic proposer runs and
the batch is labelled `proposer=rules` — that is the control arm for the LLM ablation, not
a degraded mode.

---

## How it works

One `rr run` walks a simulated IST day clock over a 30-day cycle. For every account still
open it does the same six things.

| | stage | what happens |
|---|---|---|
| 1 | **Detect** | A mandate debit failed. The agent sees the NPCI return code, the amount, the city tier and the attempt history — never the customer's balance. A boundary test enforces that. |
| 2 | **Assign** | The account is randomised into treatment or holdout, stratified by cause × amount band × merchant category, and written to the ledger *first*. Each account has its own RNG stream, so the control arm cannot depend on what the treatment arm did. |
| 3 | **Diagnose** | A Bayesian posterior over seven cause classes. Return codes are noisy (12% by config), so this is a distribution, never a label. |
| 4 | **Narrow** | Structural facts remove actions. Over the per-transaction ceiling, `RETRY_DEBIT` is removed and `SPLIT_DEBIT` substituted. An action the gate will certainly refuse is not eligible. |
| 5 | **Price** | A finite-horizon budgeted MDP over (attempts, contacts, notice pending, days to inflow, mandate health, alternate rail), solved exactly by backward induction. `CLOSE` is worth zero, which is what makes stopping economic. |
| 6 | **Gate → execute** | 29 pure-function rules. Only a `GateDecision` with `verdict == ALLOW` **whose `action_hash` matches the action** reaches a rail — and the adapter enforces that itself. |

Self-pay then rolls for every unsettled cycle, *including ones the agent has closed* —
stopping the workflow is the agent declining to spend, not the customer declining to pay.
At the horizon every cycle closes with exactly one terminal state.

### Where the value is, and where it is not

| cause | n | agent | holdout | lift | value |
|---|---:|---:|---:|---:|---:|
| INSUFFICIENT_FUNDS | 1,162 | 61.2% | 41.2% | +20.0% | ₹10,00,866 |
| TRANSIENT_INFRA | 691 | 82.6% | 57.3% | +25.2% | ₹6,52,475 |
| MANDATE_REVOKED | 188 | 36.4% | 23.9% | +12.5% | ₹80,255 |
| LIMIT_EXCEEDED | 216 | 23.8% | 13.5% | +10.2% | ₹96,549 |
| MANDATE_INVALID | 259 | 22.7% | 14.7% | +7.9% | ₹70,639 |
| AUTH_ARTEFACT | 330 | 25.0% | 22.6% | +2.4% | ₹30,144 |
| **ACCOUNT_TERMINAL** | 154 | 20.0% | 35.6% | **−15.6%** | **−₹1,15,522** |

The last row is on the scoreboard because taking it off would make the headline a lie: on
closed and blocked accounts the agent is **worse than doing nothing**.

---

## Compliance is a pure function, not a prompt

29 versioned rules in [`app/policy/rules.py`](app/policy/rules.py), each carrying the
regulation it implements. `rr rules` prints the catalogue with its version hash.

| family | n | enforces |
|---|---:|---|
| `STOP` | 5 | opt-out, dispute, subjudice, bereavement, terminal state |
| `QH` | 3 | 08:00–19:00 window, Sundays and gazetted holidays, festival dates |
| `NOTICE` | 3 | 24h pre-debit notice, mandated fields, one notice per presentation |
| `AI` | 4 | disclosure, recording consent, human override, no re-entry after escalation |
| `FREQ` | 3 | 3 contacts a week, 1 a day, 1 automated voice call a cycle |
| `AFA` | 2 | the ₹15,000 / ₹1,00,000 ceiling; fresh authorisation to move a mandate |
| `BUDGET` | 2 | an attempt per presentation, spend within the cap |
| `DLT` `NUM` `CONSENT` `PURPOSE` `AMT` `PTP` | 7 | registered templates, number series, consented channels, purpose limitation, amount reconciliation, open promises |

Sources: RBI Digital Payments e-mandate framework, RBI draft recovery-agent norms, RBI
FREE-AI recommendations, TRAI TCCCPR/DLT, DPDP Act — cited per rule in
[`docs/12-GLOSSARY.md`](docs/12-GLOSSARY.md).

---

## What is real and what is simulated

| component | status |
|---|---|
| Rails, measured batch | **Simulated.** `app/sim/rails.py` implements `RailAdapter`. |
| Rails, live slice | **Real Razorpay test mode.** `app/rails/razorpay.py` — same interface, same gate, same ledger. Real payment links, real payment ids, real return codes. |
| Return codes | **Real taxonomy** (NPCI AP-series), simulated occurrence. |
| Compliance rules | **Real**, encoded from published RBI/TRAI/DPDP requirements. |
| Customer behaviour | **Simulated** from a declared generative model with a fixed seed. |
| Ledger, gate, planner, evaluator | **Real code.** This is the deliverable. |

**Why the measurement stays simulated:** you cannot randomise a control group against a
live provider, and you cannot authorise thousands of mandates in a browser — RBI requires
additional-factor authentication on every mandate registration, which is a human in a
browser by design. The live slice therefore records `randomised:false` on its `ASSIGN`
event and is excluded from the measured comparison by construction. Both halves, labelled.

### Running against Razorpay test mode

Put `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env`. **The adapter refuses any key
not starting `rzp_test_`** — a live key would move real money on behalf of real customers.

```bash
rr simulate --accounts 200 --seed 7 --out data/live.db
rr live --batch data/live.db --accounts 5 --dry-run   # gate everything, call nothing
rr live --batch data/live.db --accounts 5             # create real payment links
rr sync --batch data/live.db                          # read Razorpay back
```

`rr sync` is the half most demos skip. A link nobody has paid produces no payment, which is
why a dashboard's Payments screen stays empty while Payment Links fills up. When one *is*
paid, the sync writes it to the ledger as a confirmed recovery — **counted from what the
provider confirms, never from the fact that we asked.**

---

## Where the LLM sits — and why it is not in the money path

The Groq proposer is consulted only where the planner has no clear winner (top two actions
within 5%). It returns an **action type** plus a rationale; amounts, schedules, templates
and recipients are filled by deterministic code, and there is nowhere in the action object
to put customer-facing text. An action outside the eligible set is discarded.

Measured, over 185 decisions it actually answered: it agreed with the planner **95.7%** of
the time and changed 8 — seven `WAIT` into a payment link, one into a message. Money
difference: **₹0**.

We measured the LLM and did not ship it in the money path. The infrastructure that made
that finding possible is what separates this from a demo with an LLM bolted to a database.

> The ablation is incomplete: it hit the free tier's 200,000 tokens-per-day wall at call
> 188 of 527 consulted decisions, so the arm is part model and part planner. The report
> says so in as many words. Quote the agreement rate, never the arm's recovery number.

---

## CLI

```
rr simulate   generate a batch of at-risk accounts
rr run        run a policy over a batch
rr report     the scoreboard
rr verify     hash chain + 8 ledger invariants
rr timeline   one account's audit trail
rr gate       evaluate an action against the gate, execute nothing
rr rules      the rule catalogue and its version hash
rr frontier   recovery/harm frontier as lambda varies
rr ablate     rules heuristic vs the MDP vs the LLM, same world, same seed
rr live       drive a slice through Razorpay test mode
rr sync       read Razorpay back: which links were actually paid
rr config     the resolved config and its hash
```

---

## Layout

```
app/
  domain/     enums, dataclasses, IDs, money, clock, config
  ledger/     append-only event store, hash chain, 8 invariants
  sim/        world model, simulated rails, account generator
  rails/      the RailAdapter Protocol and the Razorpay test adapter
  diagnose/   code map, cause posterior, eligible set
  policy/     29-rule catalogue and the evaluation gate
  plan/       budgeted MDP, solved by backward induction
  propose/    Groq proposer and deterministic fallback
  eval/       metrics, bootstrap CIs, report, ablation, λ frontier
  api/        FastAPI app
  live.py     drive a slice through real Razorpay test mode
  sync.py     read Razorpay back
  runner.py   the day loop and the executor
  policies.py merchant default, fixed schedule, the agent
config/       default.yaml + codemap.yaml — every threshold lives here
web/          React + Vite dashboard
tests/        339 tests
docs/         the spec, read in order; DECISIONS.md is the engineering log
```

---

## Invariants, enforced by tests

If a change breaks one of these, the change is wrong.

1. **Nothing reaches a rail without a passing policy evaluation**, and the evaluation is
   written to the ledger whether it passed or failed.
2. **Arm assignment is written before any other event for that account.** Assigning or
   reading the arm later is how holdouts get silently corrupted.
3. **The ledger is append-only.** Corrections are new events. No `UPDATE`, no `DELETE`.
4. **The LLM never emits an amount, a phone number, or customer-facing text.**
5. **Every action has a cost and a harm weight.** An action with neither cannot be
   scheduled, because the planner cannot price it.
6. **`WAIT` is a real action.** An agent that cannot choose to do nothing is not optimising.
7. **One `Clock`, simulated IST.** Nothing calls `datetime.now()` outside
   `app/domain/clock.py`.
8. **Money is integer paise**, formatted only at the edge.
9. **Every action the planner can choose has an execution path.** Falling off the end of
   the dispatch is recorded as `NOT_EXECUTED`, never dropped.

```bash
pytest -q      # 339 passed
```

---

## Known limitations

Stated here rather than discovered later.

- **The lift is concentrated in one mechanism.** Disabling `SPLIT_DEBIT` drops the result
  to ₹1,31,826 with an interval containing zero. 358 of 3,000 accounts (11.9%) sit above
  their binding per-transaction ceiling with a mean amount 3.1× the batch mean, and before
  the split existed those accounts had no debit action at all. The A/B is reproducible.
- **The agent destroys value on `ACCOUNT_TERMINAL`** — −15.6%, costing ₹1,15,522.
- **3,000 accounts is underpowered** for a point estimate; one seed in four has an interval
  containing zero.
- **The hardship detector is precise rather than complete** — precision 0.75, recall 0.11.
  A false hardship exit stops a legitimate recovery, so the threshold is set deliberately.
- **The live slice exercises payment links only.** Mandate registration and recurring debit
  are implemented and gate-enforced, but charging a mandate needs a token, and a token
  needs a human to complete an authorisation in a browser.

---

## Documentation

| # | Doc | |
|---|---|---|
| 00 | [Overview](docs/00-OVERVIEW.md) | Problem, thesis, non-goals, regulatory basis |
| 01 | [Domain model](docs/01-DOMAIN-MODEL.md) | Enums, entities, IDs, money, time — the shared contract |
| 02 | [Ledger](docs/02-LEDGER.md) | Event schema, hash chain, invariants |
| 03 | [Simulator](docs/03-SIMULATOR.md) | Latent state, rail behaviour, self-cure |
| 04 | [Cause taxonomy](docs/04-CAUSE-TAXONOMY.md) | Return codes → causes → posterior |
| 05 | [Policy engine](docs/05-POLICY-ENGINE.md) | The rule catalogue and the gate |
| 06 | [Planner](docs/06-PLANNER.md) | Budgeted MDP, value iteration, stopping |
| 07 | [Proposer](docs/07-PROPOSER-GROQ.md) | LLM call, schema, fallback, ablation |
| 08 | [Evaluation](docs/08-EVALUATION.md) | Arms, metrics, confidence intervals |
| 09 | [API & CLI](docs/09-API.md) | Endpoints, commands, response shapes |
| 11 | [Demo](docs/11-DEMO.md) | Six-minute script and what to say |
| 12 | [Glossary](docs/12-GLOSSARY.md) | Terms, citations, verification checklist |
| — | [**DECISIONS.md**](docs/DECISIONS.md) | The engineering log: every non-obvious call and why |

`DECISIONS.md` is the most useful file here if you want to know whether the numbers can be
trusted. It records the bugs that made earlier results wrong — a bootstrap that
manufactured significance, a shared RNG that coupled the two arms, an LLM ablation that was
measuring a rate limiter — and what was done about each.

---

## Notes

The regulatory positions in `docs/12-GLOSSARY.md` are stated as of **September 2026** and
some rest on secondary commentary. Verify the flagged items against primary sources before
presenting them as settled.
