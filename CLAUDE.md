# CLAUDE.md — instructions for the coding agent

You are implementing the spec in `docs/`. Read `docs/01-DOMAIN-MODEL.md` first; it is
the shared contract. Everything else refers to its enums and field names.

## Order of work

Build in the order given in `docs/10-BUILD-PLAN.md`. The order is deliberate: the
measurement spine exists before the agent, so that if you run out of time you stop
with a smaller agent and an intact measurement story, never the reverse.

Do not skip ahead to the planner or the LLM. A working ledger + simulator + evaluator
with a do-nothing policy is a complete, presentable deliverable. A clever planner with
no holdout is not.

## Hard rules

1. **Append-only ledger.** `app/ledger/` exposes `append(event)` and read queries.
   It exposes no update or delete. If you need to correct something, append a
   correction event referencing the original `event_id`.

2. **The gate is the only path to a rail.** `RailAdapter` methods must refuse to run
   unless handed a `GateDecision` with `verdict == ALLOW` whose `action_hash` matches
   the action being executed. Enforce this in the adapter, not by convention.

3. **Arm assignment happens once, first, and is immutable.** Write the `ASSIGN` event
   before `DIAGNOSE`. Any code path that reads `arm` to decide behaviour outside the
   evaluator is a bug — the *policy* differs by arm, the *plumbing* does not.

4. **No LLM in the money path.** The proposer returns an `ActionType` plus a
   `rationale` string plus `evidence_refs`. Amounts, schedules, templates and
   recipients are filled by deterministic code. If the proposer returns an action not
   in the eligible set, discard it, log `PROPOSER_INVALID`, and fall through to the
   deterministic proposer. Never retry the LLM in a loop.

5. **Determinism.** Every run takes a seed. Same seed + same config + same policy
   version ⇒ byte-identical ledger except for `wall_clock_at`. Put the seed in the
   batch metadata. Tests depend on this.

6. **Simulated time, not real time.** There is one `Clock` object. Nothing calls
   `datetime.now()` outside `app/domain/clock.py`. Compliance rules like quiet hours
   are evaluated against the simulated clock in IST.

7. **Money is integer paise.** No floats for money, anywhere. Format only at the edge.

8. **Config, not constants.** Costs, harm weights, hazard rates, thresholds all live in
   one `Config` dataclass loaded from `config/default.yaml`. A number hardcoded in a
   module is a bug — the demo needs to show the λ frontier by varying config.

## Style

- Python 3.11+, `from __future__ import annotations`, full type hints.
- Dataclasses (frozen where possible) over dicts for domain objects.
- Pure functions for anything testable: `evaluate(action, context) -> GateDecision`,
  `posterior(code, history) -> dict[CauseClass, float]`, `plan(state) -> Action`.
  Side effects live in thin orchestration modules.
- No ORM. Hand-written SQL in `app/ledger/store.py`. The schema is small and the
  queries are the interesting part.
- Log with `structlog` or stdlib `logging` at INFO for stage transitions only; the
  ledger is the real record, logs are for you.

## Testing

Every milestone in `docs/10-BUILD-PLAN.md` has acceptance tests. Write them as you go,
not after. Minimum set:

- `test_ledger_chain.py` — tampering with any event breaks `verify()`.
- `test_arm_immutable.py` — assigning an arm twice raises.
- `test_gate_denies.py` — one test per rule ID in `docs/05-POLICY-ENGINE.md`.
- `test_notice_coupling.py` — a `RETRY_DEBIT` without a ≥24h prior notice is denied.
- `test_selfcure.py` — the do-nothing policy recovers a non-trivial share.
  **If this test shows near-zero self-cure, the simulator is lying and every
  downstream number is inflated.** Treat a failure here as a stop-the-line event.
- `test_planner_waits.py` — with an insufficient-funds cause and inflow 3 days out,
  the planner chooses `WAIT`, not `RETRY_DEBIT`.
- `test_no_llm_amounts.py` — the proposer schema has no amount field.

## What not to build

- No auth, no multi-tenancy, no user accounts.
- No real payment integration, no real SMS/voice sending. Adapters are simulated.
- No React dashboard until everything in `docs/10-BUILD-PLAN.md` M1–M6 is green.
  The FastAPI JSON endpoints plus `rr report` are enough to demo.
- No retry-timing ML model. That is explicitly not the thesis; see `docs/00-OVERVIEW.md`.
- No prompt-based compliance. Rules go in `app/policy/rules.py`.

## When the spec is ambiguous

Prefer the choice that makes the measurement more honest, then the one that is easier
to explain in a demo, then the one that is less code. Write the decision as a short
note in `docs/DECISIONS.md` with the date and the reasoning — that file is also your
evidence of engineering judgement when someone asks why.
