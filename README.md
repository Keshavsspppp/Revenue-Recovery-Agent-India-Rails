# Revenue Recovery Agent — India Rails

An agent that detects revenue at risk on Indian payment rails, diagnoses the cause,
chooses a bounded intervention, executes it under a compliance gate, and **proves how
much of the recovered money was actually caused by the agent.**

The claim this project makes is not "we recovered ₹X." It is:

> Across a batch of N at-risk accounts, with a randomised holdout arm, the agent
> recovered **₹X more than the accounts would have recovered on their own**,
> at a cost of ₹Y, with Z opt-outs per thousand accounts, and every decision is
> reconstructable from an append-only ledger.

Filled in, on 3,000 accounts at seed 42 — regenerate it in three commands below:

| | |
|---|---|
| agent recovered | ₹63,27,406 · 52.3% of ₹1,49,73,344 at risk |
| **holdout recovered on its own** | **₹45,88,019 · 38.0%** |
| incremental | **₹17,39,386**, 95% CI [₹7,32,133, ₹26,69,097] |
| opt-outs per 1,000 | 6.7, against the merchant baseline's 16.6 |
| notice-window violations | 0 |

Four seeds: positive on all four, interval clear of zero on three. The spread is
₹7.2L–₹19.5L, so quote the range rather than one seed's point estimate.
[`docs/11-DEMO.md`](docs/11-DEMO.md) has the replication table and what to say.

---

## The thesis in one paragraph

On Indian rails, retry *timing* is not the binding constraint — RBI's consolidated
e-mandate framework requires a pre-debit notification at least 24 hours before each
debit. That makes every attempt a pre-announced, expensive, budgeted event rather
than a free loop. So the problem is not "predict the best retry day" (Stripe already
does that with a 500-feature ensemble). The problem is **allocating a tiny budget of
expensive actions** — attempt, notify, message, repair the mandate onto a different
rail, or deliberately wait for the customer's salary date — under hard compliance
constraints, with a stopping rule that has an economic basis.

Full reasoning: [`docs/00-OVERVIEW.md`](docs/00-OVERVIEW.md).

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Numeric planner + eval harness |
| API | FastAPI + Uvicorn | Fast to stand up, auto docs for the demo |
| Store | SQLite (WAL) | Zero-setup, append-only ledger, ships in the repo |
| Numerics | numpy | Simulator, value iteration, vectorised bootstrap CIs |
| LLM | Groq (OpenAI-compatible) | Structured outputs; deterministic fallback when no key |
| Dashboard | React + Vite + TypeScript | Typed against the API; each panel fetches independently |
| Tests | pytest | 339 tests; boundary tests guard the architecture rules |

No Postgres, no Celery, no queue. Time is simulated, so the scheduler is a loop.

---

## Quickstart

```bash
git clone <your-repo> revenue-recovery && cd revenue-recovery
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api,dev]"    # api = FastAPI + uvicorn, dev = pytest

cp .env.example .env          # Groq and Razorpay keys — both optional

# 1. Generate a batch of at-risk accounts
rr simulate --accounts 3000 --seed 42 --out data/demo.db

# 2. Run the agent against a randomised 20% holdout
rr run --batch data/demo.db --policy agent --holdout 0.2

# 3. Print the scoreboard
rr report --batch data/demo.db

# 4. Verify the hash chain and all eight ledger invariants
rr verify --batch data/demo.db

# One policy per batch: the world is regenerated from the seed and the ledger is
# append-only, so a second run cannot be reconciled with the first. To compare
# baselines, simulate to a fresh path with the same seed.
rr simulate --accounts 3000 --seed 42 --out data/floor.db
rr run --batch data/floor.db --policy nothing

# 5. Build the dashboard and serve everything from one process
cd web && npm install && npm run build && cd ..
uvicorn app.api.main:app --port 8010    # then open http://127.0.0.1:8010
```

The page at `/` is the whole story on one screen: the holdout gap with its confidence
interval drawn on an axis with zero marked, the per-cause cut including the causes where
the agent destroys value, a compliance gate you can push a time into, the harm counters
beside the money, one account's full ledger trail, and the real Razorpay objects. Every
section names the endpoint it was built from, because the argument this project makes is
"do not trust the number, check the trail". `/docs` is the OpenAPI surface, and it works
with or without the dashboard.

For frontend work, `npm run dev` serves on :5173 with hot reload and proxies the API.

### Against real Razorpay test mode

Put `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env`. The adapter refuses any key not
starting `rzp_test_` — a live key would move real money on behalf of real customers.

```bash
rr simulate --accounts 200 --seed 7 --out data/live.db
rr live --batch data/live.db --accounts 5 --dry-run   # gate everything, call nothing
rr live --batch data/live.db --accounts 5             # create real payment links
rr sync --batch data/live.db                          # read back which were paid
```

`rr sync` is the half most demos skip: a link nobody has paid produces no payment, so the
dashboard's Payments screen stays empty while Payment Links fills up. When one *is* paid,
the sync writes it to the ledger as a confirmed recovery — counted from what the provider
confirms, never from the fact that we asked.

**The repo must run end-to-end with no `GROQ_API_KEY`.** Without a key the
deterministic proposer is used and the run is labelled `proposer=rules` in the
ledger. This is not a degraded mode — it is the control arm for the LLM ablation.

---

## Repo layout

```
revenue-recovery/
├── README.md
├── CLAUDE.md                  # instructions for the coding agent
├── pyproject.toml             # `rr` console script, optional [api] and [dev] extras
├── .env.example
├── config/                    # default.yaml + codemap.yaml — every threshold lives here
├── app/
│   ├── domain/                # enums, dataclasses, IDs, money, clock, config
│   ├── ledger/                # append-only event store + hash chain + 8 invariants
│   ├── sim/                   # world model, simulated rails, account generator
│   ├── rails/                 # the RailAdapter Protocol and the Razorpay test adapter
│   ├── diagnose/              # code map (L1), cause posterior (L2), eligible set
│   ├── policy/                # 29-rule catalogue + the evaluation gate
│   ├── plan/                  # budgeted MDP, solved by backward induction
│   ├── propose/               # Groq proposer + deterministic fallback
│   ├── eval/                  # metrics, bootstrap CIs, report, ablation, λ frontier
│   ├── api/                   # FastAPI app; `web/` holds the built dashboard
│   ├── live.py                # drive a slice through real Razorpay test mode
│   ├── sync.py                # read Razorpay back: which links were actually paid
│   ├── runner.py              # the day loop and the executor
│   ├── policies.py            # merchant default, fixed schedule, the agent
│   └── cli.py
├── web/                       # React + Vite dashboard (source; build is gitignored)
├── tests/                     # 339 tests
├── data/                      # generated batches (gitignored, regenerable from a seed)
└── docs/                      # ← the spec, read in order
```

---

## Documentation index

Read in order. Each doc is written so it can be implemented without reading the others,
but the enums and field names in `01-DOMAIN-MODEL.md` are the contract everything shares.

| # | Doc | What it fixes |
|---|---|---|
| 00 | [Overview](docs/00-OVERVIEW.md) | Problem, thesis, non-goals, regulatory basis |
| 01 | [Domain model](docs/01-DOMAIN-MODEL.md) | **Enums, entities, IDs, money, time — the shared contract** |
| 02 | [Ledger](docs/02-LEDGER.md) | Event schema, hash chain, SQLite DDL, invariants |
| 03 | [Simulator](docs/03-SIMULATOR.md) | Latent customer state, rail behaviour, self-cure |
| 04 | [Cause taxonomy](docs/04-CAUSE-TAXONOMY.md) | Return codes → canonical causes → posterior |
| 05 | [Policy engine](docs/05-POLICY-ENGINE.md) | Rule catalogue with IDs; the compliance gate |
| 06 | [Planner](docs/06-PLANNER.md) | Budgeted EV formulation, value iteration, stopping |
| 07 | [Proposer (Groq)](docs/07-PROPOSER-GROQ.md) | LLM call, JSON schema, fallback, ablation |
| 08 | [Evaluation](docs/08-EVALUATION.md) | Arms, metric formulas, confidence intervals |
| 09 | [API & CLI](docs/09-API.md) | Endpoints, CLI commands, response shapes |
| 10 | [Build plan](docs/10-BUILD-PLAN.md) | Milestones with acceptance tests |
| 11 | [Demo](docs/11-DEMO.md) | Six-minute script and what to say |
| 12 | [Glossary & sources](docs/12-GLOSSARY.md) | Terms, citations, verification checklist |

---

## What is real and what is simulated

Be explicit about this everywhere — in the README, in the demo, in the repo.
Volunteering it is what keeps the room's trust.

| Component | Status |
|---|---|
| Payment rails (the measured batch) | **Simulated.** `app/sim/rails.py` implements `RailAdapter`. |
| Payment rails (the live slice) | **Real Razorpay test mode.** `app/rails/razorpay.py`, same interface, same gate, same ledger. Real payment links, real payment ids, real return codes. |
| Return codes | **Real taxonomy**, simulated occurrence. AP-series from NPCI's revised code list. |
| Compliance rules | **Real rules**, encoded from published RBI/TRAI requirements. |
| Customer behaviour | **Simulated** from a declared generative model with a fixed seed. |
| Ledger, gate, planner, evaluator | **Real code.** These are what you are actually building. |
| Money recovered | **Simulated rupees.** The *methodology* for measuring it is the deliverable. |

Swapping the simulator for a sandbox is a one-file change behind `RailAdapter`, and this
repo cashes that claim rather than asserting it: `rr live` drives a small slice through
Razorpay test mode and `rr sync` reads back which links were actually paid.

**Why the measurement stays simulated:** you cannot randomise a control group against a
live provider, and you cannot authorise thousands of mandates in a browser — RBI requires
additional-factor authentication on every mandate registration, which is a human in a
browser by design. So the live slice records `randomised:false` on its `ASSIGN` event and
is excluded from the measured comparison by construction. Both halves, labelled.

---

## Non-negotiable invariants

These are enforced by tests. If a change breaks one, the change is wrong.

1. **Nothing reaches a rail adapter without a passing policy evaluation**, and the
   evaluation is written to the ledger whether it passed or failed.
2. **Arm assignment is written before any other event for that account.**
   Assigning or reading the arm later is how holdouts get silently corrupted.
3. **The ledger is append-only.** Corrections are new events. No `UPDATE`, no `DELETE`.
4. **The LLM never emits an amount, a phone number, or customer-facing text.**
   It selects one action from a pre-computed eligible set.
5. **Every action has a cost and a harm weight.** An action with neither cannot be
   scheduled, because the planner cannot price it.
6. **`WAIT` is a real action.** An agent that cannot choose to do nothing is not
   optimising.
7. **One `Clock`, simulated IST.** Nothing calls `datetime.now()` outside
   `app/domain/clock.py`; `wall_clock()` is the single permitted reader and only
   provenance consumes it.
8. **Money is integer paise**, formatted only at the edge.
9. **Every action the planner can choose has an execution path.** Falling off the end of
   the dispatch is recorded as `NOT_EXECUTED`, never dropped — an action that silently
   does nothing is a free `WAIT` the value function still prices.

---

## Licence & attribution

The regulatory positions cited in `docs/12-GLOSSARY.md` are stated as of **September 2026**
and some rest on secondary commentary. Verify the flagged items against primary
sources before presenting them as settled.
