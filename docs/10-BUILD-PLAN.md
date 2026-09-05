# 10 — Build plan

Ordered so that the thing which *proves* the claim exists before the thing which *makes*
the claim. If you run out of time you stop with a smaller agent and an intact
measurement story — never the reverse.

Each milestone has an acceptance test. Do not start the next one until the current one
is green. The hour estimates assume one person; halve them for two if you split at M3.

---

## M0 · Skeleton — 1h

Repo, `pyproject.toml`, `config/default.yaml`, `Config` dataclass, `Clock`, enums and
dataclasses from `01-DOMAIN-MODEL.md`, `pytest` wired up.

**Accept:** `pytest` runs and collects zero failures. `rr --help` works.

---

## M1 · Ledger — 2h

`append`, hash chain, SQLite DDL, `verify()`, the four read queries.

**Accept:** `test_chain_detects_tamper` — mutate one byte of a `payload` in the DB and
`verify()` names the exact `seq`. `test_arm_written_once` raises on a second `ASSIGN`.

> Everything writes here from the first commit. Retrofitting an audit trail is how
> projects lose their audit trail.

---

## M2 · Simulator — 3h

Latent accounts, balance dynamics, `RailAdapter` with the attempt-resolution order, code
distribution, contact effects, self-cure via the merchant default policy plus the
self-pay hazard.

**Accept:** `test_selfcure` — do-nothing holdout recovery lands in **0.30–0.50** of
at-risk value on a 2,000-account batch. `test_boundaries` — no agent module transitively
imports `app.sim`.

> `test_selfcure` is **stop-the-line**. Near-zero self-cure means the simulator is lying
> and every downstream number is inflated.

---

## M3 · Arms + evaluator — 3h

Stratified assignment, the three denominators, incremental rate, bootstrap CI, harm
counters, `rr report`.

**Accept:** `rr run --policy nothing` then `rr report` prints a complete scoreboard with
a real confidence interval. `test_report_matches_ledger` — every figure is recomputable
from raw events by an independent query.

**This is the first genuinely presentable deliverable.** From here on, every milestone is
an improvement to a system that already produces a defensible number.

---

## M4 · Rail adapters + executor — 2h

Idempotency keys per `(account, cycle, action_type, day)`, notice receipts, settlement
feed, `GateDecision` enforcement inside the adapter.

**Accept:** `test_idempotent_execute` — running the same action twice produces one
attempt and one ledger `EXECUTE`. `test_adapter_refuses_without_gate` raises
`GateViolation`.

---

## M5 · Policy gate — 3h

Every rule in `05-POLICY-ENGINE.md`, the `Rule` registry, `policy_version` hashing,
`POST /policy/evaluate`.

**Accept:** one passing test per rule ID. `test_all_actions_covered`.
`test_policy_version_bumps` fails CI when a rule changes without a version bump.

---

## M6 · Cause taxonomy — 2h

Layer 1 code map from `config/codemap.yaml`, the cause → eligible action matrix, the
hardship and open-PTP overlays. Layer 2 posterior only if M1–M5 are green.

**Accept:** `test_eligible_excludes_wrong` — `MANDATE_INVALID` never yields
`RETRY_DEBIT` on the broken rail; `TRANSIENT_INFRA` never yields `SEND_MESSAGE`.
`test_unmapped_counted` — an unknown code maps to `UNKNOWN` and increments the counter.

---

## M7 · Baseline policies — 1h

`nothing`, `fixed`, `oracle`.

**Accept:** all three run end to end and `rr report` shows `fixed` beating `nothing`, and
`oracle` beating both. If `fixed` does not beat `nothing`, something is wrong upstream —
investigate before proceeding.

---

## M8 · Planner — 4h

Inflow phase estimation (circular mean), the factorised success model, backward
induction over `PlanState`, notice coupling, economic stopping, the λ term.

**Accept:** `test_planner_waits`, `test_planner_repairs`, `test_stopping_is_economic`,
`test_notice_coupling`, `test_lambda_monotone`. And the one that matters:
`rr report` shows `agent` beating `fixed` with a confidence interval that excludes zero.

> If the interval straddles zero, do not tune until it does not. Report it honestly, then
> look at the per-cause cuts to find out *where* the agent is failing to add value. That
> investigation is a better result than a tuned number.

---

## M9 · PTP state machine — 1.5h

`PromiseToPay` object, `OPEN → KEPT | BROKEN | PARTIAL | LAPSED`, auto-resolution against
settlement, outreach suppression, fatigue reset on `KEPT`, confidence decay on `BROKEN`.

**Accept:** `test_ptp_suppresses` — no contact between capture and
`promised_date + grace`. `test_ptp_resolves` — all four transitions fire correctly.

Cheap, high-signal, and the clearest possible evidence of a genuinely *bounded* workflow.

---

## M10 · Groq proposer — 2h

Client, strict JSON schema, prompt, caching, one-retry-then-fall-through, the
deterministic `RulesProposer`, `rr ablate`.

**Accept:** `test_no_llm_amounts` — the schema contains no amount, schedule, template,
recipient or message-body field. The repo runs end to end with `GROQ_API_KEY` unset.
`rr ablate` prints all three runs with intervals.

---

## M11 · Hardship + λ frontier — 2h

Observable hardship signals, `hardship_score`, the accommodation exit, `rr frontier`.

**Accept:** hardship detector precision/recall reported against `latent_truth`.
`rr frontier` shows contacts falling monotonically as λ rises.

---

## M12 · Demo surface — 2h

`GET /batches/{id}/scoreboard`, the timeline endpoint, the live-denial form, and the
single-file HTML page.

**Accept:** you can run the whole `11-DEMO.md` script end to end without touching a
terminal.

---

## M13 · Voice, or not — 3h, optional

One scripted PTP-confirmation flow with disclosure, recording consent, DTMF fallback and
human routing.

**Accept:** the flow completes, and every one of `POL-AI-001`, `POL-AI-002`, `POL-QH-001`
is demonstrably enforced.

> **Cut this without regret if M1–M12 are not comfortably done.** A well-measured
> messaging-plus-retry agent beats a voice demo that fails live.

---

## Optional · 43B(h) receivables action — 2h

Only after M12. Add `SEND_MSME_EXPOSURE_NOTICE`: an eligibility check (Udyam-registered
micro or small enterprise; traders excluded; written agreement or not) and a computed,
dated statement of the buyer's tax exposure — deduction deferral plus compound interest
at three times the RBI bank rate. Arithmetic, not persuasion, and fully auditable.

---

## Cut list, in order

When time runs short, cut from the bottom. Nothing above a cut may be compromised to
save something below it.

1. Voice (M13)
2. 43B(h) action
3. The HTML page (M12) — `rr report` and `/docs` are enough
4. Layer 2 cause posterior (keep Layer 1 — the deterministic map is defensible alone)
5. Groq proposer (M10) — `rules` is the control arm and ships on its own
6. Hardship detector (M11)

**Never cut:** the ledger, the holdout, the gate, or the confidence intervals. Those four
*are* the project.

---

## Definition of done

- [ ] `rr verify` exits 0 on a full batch
- [ ] `rr report` prints incremental recovery with a 95% CI
- [ ] Every rule ID has a passing test
- [ ] The holdout arm has zero non-default executions
- [ ] `notice_window_violations == 0`
- [ ] The ablation table exists, whatever it says
- [ ] README states plainly what is simulated and what is real
- [ ] `docs/DECISIONS.md` records every ambiguity you resolved and why
