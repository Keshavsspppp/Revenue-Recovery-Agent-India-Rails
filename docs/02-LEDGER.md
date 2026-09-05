# 02 — Ledger

Module: `app/ledger/`

A log of API calls is not an audit trail. The question a regulator, an ombudsman or an
angry customer asks is *"why did this system call me at 6:47pm on the 3rd, twice?"* —
and it gets asked months later, when the policy and the model have both changed. The
ledger must answer that with the exact versions that were live at the time.

Build this first. Retrofitting an audit trail is how projects lose their audit trail.

---

## Design

- **Append-only.** No `UPDATE`, no `DELETE`. Corrections are new events referencing the
  original `event_id`. The store exposes `append()` and read queries; that is the whole
  write surface.
- **Hash-chained** per batch, so tampering is detectable.
- **Event-sourced.** Current state is a fold over events. Materialised views are caches
  and may be rebuilt from the log at any time; `rr rebuild` must reproduce them exactly.
- **`decision_id` stitches a lifecycle.** One decision spans DIAGNOSE → ELIGIBLE →
  PROPOSE → GATE → EXECUTE → OBSERVE. That grouping is what makes the trail *answerable*
  rather than merely complete.

---

## Event schema

One row per stage. Stored as a JSON blob plus indexed columns.

```json
{
  "event_id":       "evt_01J8Z3QK9M2R7X",
  "prev_hash":      "sha256:9f2c1a...",
  "hash":           "sha256:4b71de...",
  "batch_id":       "bat_001",
  "seq":            48211,
  "occurred_at":    "2026-09-03T13:12:04+05:30",
  "wall_clock_at":  "2026-09-03T07:42:11+00:00",

  "account_id":     "acc_8831",
  "cycle_id":       "cyc_8831_09",
  "decision_id":    "dec_5521",
  "stage":          "EXECUTE",

  "action":         {"type":"RETRY_DEBIT","rail":"UPI_AUTOPAY","amount_paise":149900,
                     "scheduled_for":"2026-09-03T13:00:00+05:30"},
  "action_hash":    "sha256:aa19...",

  "cause_posterior":{"INSUFFICIENT_FUNDS":0.71,"TRANSIENT_INFRA":0.19,"UNKNOWN":0.10},
  "evidence":       ["rail_code:AP01","prior_success_phase:day_2",
                     "attempts_made:1","days_to_est_inflow:0"],

  "policy":         {"version":"pol_2026.09.1",
                     "checks_passed":["POL-QH-001","POL-NOTICE-001","POL-FREQ-001",
                                      "POL-CONSENT-001"],
                     "check_failed": null},

  "model":          {"planner":"budget_vi_0.4.2",
                     "cause":"cause_lr_0.2.0",
                     "proposer":"groq:openai/gpt-oss-120b"},

  "arm":            "treatment",
  "budgets_before": {"attempts_remaining":3,"contacts_remaining_week":2,
                     "voice_remaining_cycle":1,"spend_remaining_paise":8000},
  "result":         {"ok":true,"rail_code":"SUCCESS",
                     "settled_at":"2026-09-03T13:12:31+05:30","fee_paise":200},
  "human_override": null,
  "outcome_ref":    "obs_9912",
  "notes":          null
}
```

### Field rules

| Field | Rule |
|---|---|
| `event_id` | ULID. Monotonic within a batch. |
| `seq` | Dense integer, gapless per batch. A gap means data loss — `verify()` fails. |
| `prev_hash` | `hash` of `seq-1` in the same batch. First event: `"sha256:genesis"`. |
| `hash` | `sha256(canonical_json(event_without_hash))`. Canonical = sorted keys, no whitespace, UTF-8. |
| `occurred_at` | **Simulated** clock, IST. This is what compliance rules are evaluated against. |
| `wall_clock_at` | Real UTC. The only field allowed to differ between two runs with the same seed. |
| `arm` | Written at `ASSIGN` and copied onto every later event for that account. Never derived at read time. |
| `action` | `null` for `DETECT`, `ASSIGN`, `DIAGNOSE`, `OBSERVE`. |
| `policy` | Required on `GATE` and `EXECUTE`. `null` elsewhere. |
| `human_override` | `null` or `{"by": "...", "at": "...", "reason": "...", "original_action": {...}}`. FREE-AI's "right to override" needs a place to live. |
| `evidence` | Free-form strings, but each must be reconstructable — a code, a count, a bucket. Never a sentence. |

---

## SQLite DDL

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    prev_hash     TEXT NOT NULL,
    hash          TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    wall_clock_at TEXT NOT NULL,
    account_id    TEXT,
    cycle_id      TEXT,
    decision_id   TEXT,
    stage         TEXT NOT NULL,
    arm           TEXT,
    action_type   TEXT,
    action_hash   TEXT,
    rule_failed   TEXT,
    settled       INTEGER,          -- 0/1/NULL, denormalised for fast scoring
    amount_paise  INTEGER,
    payload       TEXT NOT NULL,    -- full canonical JSON
    UNIQUE (batch_id, seq)
);

CREATE INDEX idx_events_account  ON events(batch_id, account_id, seq);
CREATE INDEX idx_events_decision ON events(decision_id);
CREATE INDEX idx_events_stage    ON events(batch_id, stage);
CREATE INDEX idx_events_rule     ON events(batch_id, rule_failed);

CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    seed         INTEGER NOT NULL,
    config_json  TEXT NOT NULL,     -- the entire resolved Config
    config_hash  TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    holdout_frac REAL NOT NULL,
    lambda_harm  REAL NOT NULL,
    status       TEXT NOT NULL      -- RUNNING | COMPLETE | TRIPPED
);
```

Storing the whole resolved config with its hash is what lets you say, six weeks later,
exactly what produced a number. Do not store a config *path*.

---

## Write API

```python
class Ledger:
    def __init__(self, db_path: str, batch_id: str) -> None: ...

    def append(self, ev: EventDraft) -> Event:
        """Assigns seq, prev_hash, hash. Single writer. Raises on any attempt
        to write an event whose account has no prior ASSIGN (except DETECT/ASSIGN)."""

    def verify(self) -> VerifyReport:
        """Recompute the chain. Reports first bad seq, gaps, and orphan decisions
        (a GATE with no PROPOSE, an EXECUTE with no ALLOW gate)."""

    def timeline(self, account_id: str) -> list[Event]: ...
    def decision(self, decision_id: str) -> list[Event]: ...
    def denials(self) -> list[Event]: ...
```

There is deliberately no `update` and no `delete`.

---

## Invariants (enforced by `verify()` and by tests)

1. `seq` is dense from 0. No gaps.
2. `prev_hash[n] == hash[n-1]`. Recomputed hashes match stored.
3. For every account: the first event is `DETECT`, the second is `ASSIGN`, and `ASSIGN`
   occurs exactly once.
4. Every `EXECUTE` has a preceding `GATE` in the same `decision_id` with
   `verdict == ALLOW` and a matching `action_hash`.
5. No `EXECUTE` exists for an account in the `holdout` arm other than the merchant
   default policy's own actions.
6. Every `CLOSE` carries a `TerminalState`, and no events follow it for that cycle.
7. `settled=1` implies `result.settled_at is not null`.
8. Every `SEND_PREDEBIT_NOTICE` referenced by a `RETRY_DEBIT` occurred ≥ `notice_hours`
   before that retry's `occurred_at`.

Invariant 4 is the one that matters most: it is the machine-checkable statement of
*"nothing reached a customer without passing the gate."*

---

## Queries the demo needs

```sql
-- Recovery by arm (the headline)
SELECT arm,
       COUNT(DISTINCT account_id)                              AS accounts,
       SUM(CASE WHEN settled=1 THEN amount_paise ELSE 0 END)   AS recovered_paise
FROM events WHERE batch_id=? AND stage='OBSERVE' GROUP BY arm;

-- Denials by rule (proof the gate is live, not decorative)
SELECT rule_failed, COUNT(*) FROM events
WHERE batch_id=? AND stage='GATE' AND rule_failed IS NOT NULL
GROUP BY rule_failed ORDER BY 2 DESC;

-- One account, end to end
SELECT seq, occurred_at, stage, action_type, rule_failed
FROM events WHERE batch_id=? AND account_id=? ORDER BY seq;

-- Terminal state distribution (the stopping-rule story)
SELECT json_extract(payload,'$.result.terminal_state') AS terminal, COUNT(*)
FROM events WHERE batch_id=? AND stage='CLOSE' GROUP BY 1 ORDER BY 2 DESC;
```

---

## Acceptance tests

- `test_chain_detects_tamper` — mutate one `payload` byte in the DB; `verify()` names
  the exact `seq`.
- `test_no_execute_without_allow` — synthesise an `EXECUTE` with no gate; `verify()` fails.
- `test_arm_written_once` — a second `ASSIGN` for the same account raises.
- `test_rebuild_is_exact` — `rr rebuild` reproduces the materialised scoreboard byte-for-byte.
- `test_notice_precedes_retry` — property test over generated timelines.
