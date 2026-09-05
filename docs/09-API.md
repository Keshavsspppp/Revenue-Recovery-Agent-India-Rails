# 09 — API & CLI

Modules: `app/api/`, `app/cli.py`

Two surfaces over the same core. The CLI is what you actually use while building; the
API is what the demo drives. Neither contains business logic — both call into
`app/eval/`, `app/plan/`, `app/policy/`.

---

## CLI

Entry point `rr` (`[project.scripts] rr = "app.cli:main"` in `pyproject.toml`).

```bash
rr simulate --accounts 2000 --seed 42 --horizon 30 --out data/batch_001.db
    # generates accounts, mandates, first failures; writes DETECT events

rr run --batch data/batch_001.db --policy agent --holdout 0.2 --lambda 0.5
    # policies: nothing | fixed | agent | oracle
    # --proposer groq|rules   (default: groq if GROQ_API_KEY set, else rules)
    # --dry-run               (gate everything, execute nothing — useful for rule work)

rr report --batch data/batch_001.db [--json] [--segment cause|amount|category|tier]

rr frontier --batch data/batch_001.db --lambdas 0,0.25,0.5,1,2
    # re-runs and prints the recovery/opt-out frontier table

rr ablate --batch data/batch_001.db
    # runs rules / planner-argmax / groq on the same seed, prints the comparison

rr verify --batch data/batch_001.db
    # hash chain + all ledger invariants; non-zero exit on failure

rr timeline --batch data/batch_001.db --account acc_8831
    # the audit trail for one account, human-readable

rr rebuild --batch data/batch_001.db
    # rebuild materialised views from events; must reproduce the report exactly
```

`rr verify` in CI. A build that cannot prove its own ledger is intact should not pass.

---

## API

FastAPI, `uvicorn app.api.main:app`. No auth — it is a local demo. Say so in the README
rather than half-building auth.

### Batches

```
POST   /batches                      → create + simulate
       {"accounts":2000,"seed":42,"horizon_days":30}
       201 {"batch_id":"bat_001","status":"READY"}

POST   /batches/{id}/run             → run a policy over the batch
       {"policy":"agent","holdout_frac":0.2,"lambda_harm":0.5,"proposer":"groq"}
       202 {"run_id":"run_004","status":"RUNNING"}

GET    /batches/{id}                 → metadata, seed, config hash, status
GET    /batches/{id}/scoreboard      → the full report as JSON (08-EVALUATION.md)
GET    /batches/{id}/frontier        → [{lambda, net_incremental_paise, opt_outs_per_1k}]
GET    /batches/{id}/ablation        → the three-way proposer comparison
```

### Audit

```
GET    /accounts/{account_id}/timeline?batch_id=
       → ordered events with stage, action, gate result, outcome

GET    /decisions/{decision_id}
       → the DIAGNOSE → ELIGIBLE → PROPOSE → GATE → EXECUTE → OBSERVE chain
         for one decision. This is the endpoint the demo clicks through.

GET    /batches/{id}/denials?rule_id=
       → every denied action, grouped by rule

GET    /batches/{id}/verify
       → {"ok":true,"events":48211,"first_bad_seq":null,"invariants":{...}}
```

### The live-denial endpoint

The one endpoint built specifically for the demo. It evaluates a hypothetical action
against the real gate **without executing anything**.

```
POST   /policy/evaluate
       {"batch_id":"bat_001","account_id":"acc_8831",
        "action":{"type":"VOICE_CONFIRM_PTP","channel":"VOICE"},
        "at":"2026-09-03T19:30:00+05:30"}

       200 {"verdict":"DENY",
            "rule_id_failed":"POL-QH-001",
            "reason":"Contact actions are permitted only between 08:00 and 19:00 IST",
            "basis":"RBI draft recovery norms — 8am–7pm calling window",
            "rule_ids_passed":["POL-STOP-001","POL-STOP-002","POL-STOP-003",
                               "POL-STOP-004","POL-STOP-005"],
            "policy_version":"pol_2026.09.1"}
```

Return `basis` alongside `reason`. Being able to point at *which regulation* produced the
refusal, live, is the difference between "we thought about compliance" and "compliance is
in the control loop."

Denials from this endpoint are written to the ledger with `stage=GATE` and a
`dry_run: true` marker, so the demo's own denial appears in the audit trail you then show.

### Rules

```
GET    /policy/rules                 → the catalogue: id, title, basis, applies_to
GET    /policy/version               → {"version":"pol_2026.09.1","rules_hash":"sha256:..."}
```

---

## Response conventions

- Money as integer paise, in fields ending `_paise`. Add a sibling `_display` string
  (`"₹5,78,200"`) only in scoreboard responses, formatted with the Indian digit grouping.
- Timestamps ISO-8601 with `+05:30`.
- Errors: `{"error": {"code":"...","message":"...","detail":{...}}}`. `GateViolation`
  returns HTTP 409 with the rule ID — a denial is not a 500.
- Long runs: `POST /batches/{id}/run` returns 202 immediately and the run proceeds in a
  background task; poll `GET /batches/{id}`. A 2,000-account run over 30 simulated days
  is seconds to low minutes, so do not build a queue.

---

## Optional: the demo page

Only after M1–M8 in `10-BUILD-PLAN.md` are green. One static HTML page served at `/`,
no build step, reading the JSON endpoints:

1. **Scoreboard** — gross vs holdout as two bars side by side, with the incremental gap
   annotated and its confidence interval drawn. This is the whole claim in one picture.
2. **λ frontier** — a small line chart, recovery against opt-outs per 1,000.
3. **Account timeline** — pick an account, see its decision chain; each row expands to
   the cause posterior and the rules that passed.
4. **Live denial** — a form that posts to `/policy/evaluate` with a time picker, so you
   can set 19:30 on stage and watch it refuse.

Keep it to one file. The dashboard is not the deliverable and must not eat the
measurement work — that ordering is the point of the build plan.
