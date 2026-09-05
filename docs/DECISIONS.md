# Decisions log

One entry per ambiguity resolved. Date, what you chose, the basis, the consequence, and
what changes if the basis turns out to be wrong.

This file is your evidence of engineering judgement when someone asks why. It is worth
more than a tidier README. Append; never rewrite history.

---

## 2026-09-04 — Per-retry pre-debit notice (the load-bearing one)

**Chose:** `POL-NOTICE-003` requires a fresh 24-hour pre-debit notice for *each* attempt
(the strict reading).

**Basis:** secondary commentary on the RBI Digital Payments E-Mandate Framework
(`12-GLOSSARY.md` V1). Primary circular text not yet read.

**Consequence:** attempts are a scarce budget. `attempts_left` defaults to 4 per cycle,
and `SEND_PREDEBIT_NOTICE` becomes a real action with real opportunity cost. This is the
whole thesis of the project.

**If falsified:** relax `POL-NOTICE-003`, raise the attempt budget, **re-run the batch**,
update the headline number, and say so on the slide. The budget framing survives either
way, because an attempt still costs a customer contact and mandate goodwill — but the
scarcity is softer and the claim must be softened with it.

---

## 2026-09-04 — Holdout receives the merchant default, not nothing

**Chose:** the holdout arm gets a pre-debit notice plus retries on day +3 and +7.

**Basis:** "nothing at all" is a straw man, and the pre-debit notice is legally mandatory
regardless of what the agent does — you cannot withhold it to make a control group.

**Consequence:** the baseline is harder to beat and the reported lift is smaller and more
honest. Some self-cure is *caused by* the mandatory notice; report that separately as
notice-attributable lift.

---

## 2026-09-04 — Posterior handling in the planner

**Chose:** solve the MDP per cause class, then take the posterior-weighted argmax over
actions, rather than adding a belief dimension to the state.

**Basis:** keeps `Q` inspectable per cause, which is what the audit trail shows. The
belief-state version is more correct and much harder to explain.

**Consequence:** slightly suboptimal under high posterior entropy. Acceptable; note it if
asked.

---

## _(template)_

## YYYY-MM-DD — Title

**Chose:**

**Basis:**

**Consequence:**

**If falsified:**

---

## 2026-09-04 — Event IDs are `seq`-derived, not wall-clock ULIDs

**Chose:** `make_id(prefix, n)` produces `evt_00000048`: prefixed, zero-padded, sortable,
derived from the batch-local sequence number rather than a ULID.

**Basis:** `02-LEDGER.md` asks for ids that are "monotonic within a batch". A ULID encodes
real time in its first 48 bits, so two runs of the same seed would produce different ids
and violate CLAUDE.md rule 5 (byte-identical ledger except `wall_clock_at`). Determinism
is the property tests depend on; lexical ULID-ness is not.

**Consequence:** ids are only unique within a batch database, which is exactly the scope
they are used at. `verify()` and every read query are batch-scoped already.

**If falsified:** if global uniqueness is ever needed, seed a ULID from the *simulated*
clock plus the seq counter — deterministic and still sortable — rather than from wall time.

---

## 2026-09-04 — Config validates that every action is priced, at load time

**Chose:** `Config.validate()` refuses to load a config missing any `ActionType` from
either `action_costs` or `harm_weights`.

**Basis:** README invariant 5 — an action with neither a cost nor a harm weight cannot be
scheduled because the planner cannot price it. A missing entry should be a startup
failure, not a `KeyError` two hours into a batch.

**Consequence:** adding an `ActionType` forces a deliberate pricing decision in
`config/default.yaml` before anything runs.

---

## 2026-09-04 — `notice_ref` added to the event schema

**Chose:** one field beyond the schema in `02-LEDGER.md`: `notice_ref`, the notice id a
debit consumes.

**Basis:** invariant 8 ("every notice referenced by a `RETRY_DEBIT` occurred ≥24h before
it") is not checkable without an explicit link. Inferring the pairing from timestamps
would make `verify()` guess, and a verifier that guesses cannot be trusted to say no.

**Consequence:** `POL-NOTICE-003` (one notice consumed by one attempt) is enforceable
from the raw ledger after the fact, not only in the live gate.

---

## 2026-09-04 — Holdout contamination is checked by action type

**Chose:** invariant 5 is enforced as "a holdout `EXECUTE` may only carry
`SEND_PREDEBIT_NOTICE` or `RETRY_DEBIT`", rather than by tagging events with an
originating policy.

**Basis:** the merchant default policy is exactly notice-plus-retry, so the action type
already identifies it. One less field to keep honest.

**If falsified:** if the merchant default ever gains a third action, this check silently
widens. Then add an explicit `origin` field rather than extending the allow-list.

---

## 2026-09-04 — Self-pay hazard calibrated from 0.015 to 0.006

**Chose:** `sim.selfpay.base = 0.006`, against the 0.015 suggested in `03-SIMULATOR.md`.

**Basis:** the acceptance band for do-nothing self-cure (0.30–0.50 of at-risk value) is a
*specified constraint* and the stop-the-line test; `base_selfpay = 0.015` is an undeclared
assumption with no citation. At 0.015 a 2,000-account batch self-cures at 0.569 — outside
the band, which would inflate the treatment arm's apparent ceiling and make the holdout
look artificially strong. At 0.006 it lands at 0.437, and 0.411–0.448 across five seeds.

**Consequence:** the decomposition is now reportable, and it is worth saying out loud:
the merchant's own default retries recover 0.271 of at-risk value on their own, and
customer self-pay adds the remaining ~0.166. Both mechanisms clear the "must contribute"
test, so the holdout is not a straw man in either direction.

**If falsified:** if a real portfolio shows a different self-pay rate, change this one
number and re-run. Nothing else in the simulator depends on it, and the sweep that
produced it is a five-line script.

---

## 2026-09-04 — The codemap lives in `app/domain/`, not `app/diagnose/`

**Chose:** `app/domain/codemap.py`, loaded from `config/codemap.yaml`, read in both
directions: `cause_of(code)` for Layer 1 diagnosis, `code_for(rail, cause)` for the
simulator.

**Basis:** `04-CAUSE-TAXONOMY.md` puts Layer 1 in `app/diagnose/`, but the simulator needs
the inverse map and `app/sim/` must not import agent modules (nor they it). Duplicating
the table in both places guarantees they drift — which is the exact failure mode the
whole section warns about.

**Consequence:** one table, one version string, loaded twice. The loader refuses a config
where two codes claim to emit the same `(rail, cause)`, so the inverse stays well defined.

---

## 2026-09-04 — `RailAdapter.attempt` takes the `Action`, not a loose amount

**Chose:** `attempt(mandate, action, at, gate)` rather than the doc's
`attempt(mandate, amount_paise, at, gate)` with a stashed `self._pending_action_hash`.

**Basis:** the gate signs an action hash, so the adapter must be handed the very action
that was signed in order to compare. Stashing the pending hash on the adapter is stateful,
order-dependent and unobservable in a stack trace; passing the action makes the check a
pure comparison of two values that arrived together.

**Consequence:** `GateViolation("action_hash mismatch")` is raised when a signed ₹1,499
becomes an executed ₹42,000. `tests/test_rails.py::test_adapter_refuses_a_swapped_action`
is that case.

---

## 2026-09-04 — The defect mix is a generation parameter, not an emergent outcome

**Chose:** draw the intended first-failure cause from `sim.defect_mix`, then arrange the
account's state (balance, mandate status, cap) to realise it.

**Basis:** `03-SIMULATOR.md` calls the mix "a calibration target for a fresh batch. State
it in the report" — that is the language of an input, not an observation. Letting the mix
fall out of whatever the rails happened to emit would make it unsteerable and would leave
the rarer causes (`MANDATE_INVALID`, `LIMIT_EXCEEDED`) too thin to measure per-cause lift.

**Consequence:** observed mix on seed 42 is within 0.01 of target on every class, and
`rr simulate` prints it so the calibration is visible rather than assumed.

---

## 2026-09-04 — `provisional_gate` is a scaffold with an expiry date

**Chose:** `app/sim/world.py` exposes `provisional_gate`, which returns an unconditional
ALLOW, so M2 can drive rails before the policy engine exists at M5.

**Basis:** the adapter refuses to act without a matching ALLOW (by design), and the build
order puts the simulator three milestones before the gate. The alternative — relaxing the
adapter's check until M5 — would weaken the one enforcement point that matters.

**Consequence:** a function that mints ALLOWs exists in the tree.
`tests/test_boundaries.py::test_provisional_gate_lives_only_in_the_simulator` fails if it
is ever referenced outside `app/sim/`. **M5 must delete it from the runner path**; it
survives afterwards only as a test fixture.

---

## 2026-09-04 — A DETECT-only account is a valid ledger state

**Chose:** `verify()` invariant 3 checks "second event is ASSIGN" only for accounts that
have more than one event.

**Basis:** `rr simulate` writes DETECT and stops; arms are assigned by `rr run`. A batch
between the two commands is a legitimate intermediate state, and `rr verify` must pass on
it. An account with exactly one DETECT has had no arm read, so nothing can have been
corrupted by its absence.

**Consequence:** the invariant still catches the failure it exists for — an account that
was acted upon without an arm — because acting on it necessarily adds a second event.

---

## 2026-09-04 — The world is regenerated from the seed, not reloaded from tables

**Chose:** `rr run` rebuilds the `World` by calling `World.generate(cfg, seed, n)` with the
seed stored in the `batches` table, rather than reconstructing latent state from
`latent_truth`.

**Basis:** determinism is already a hard requirement (CLAUDE.md rule 5), so the seed *is*
a complete description of the world. Reloading would mean a second serialisation path that
could silently disagree with the generator.

**Consequence:** running a batch under a different config than it was simulated under
would compare two different worlds. `run_batch` therefore refuses on a `config_hash`
mismatch with an explicit `ConfigDrift` error naming both hashes. The `latent_truth` table
stays purely for the evaluator's ground-truth scoring.

---

## 2026-09-04 — Holdout costs are rate-adjusted before differencing

**Chose:** `net_incremental = incremental_recovered − (cost_T − cost_H × at_risk_T/at_risk_H)`,
against the literal `cost_T − cost_H` in `08-EVALUATION.md`.

**Basis:** the arms are deliberately unequal (1,592 vs 408 accounts). Differencing raw
totals compares a 1,592-account bill against a 408-account one and would report a spurious
cost of recovery roughly equal to the treatment arm's entire spend. The holdout's *recovery*
is already rate-adjusted in the same report; its cost has to be adjusted the same way or
the two halves of the subtraction are on different bases.

**Consequence:** on the A/A run the cost delta comes out at −₹155 on ₹1.01 crore at risk,
which is the right order of magnitude for two identical policies.

---

## 2026-09-05 — The stratified bootstrap pools thin strata

**Chose:** cells below `MIN_STRATUM = 25` accounts are pooled within their arm before
resampling.

**Basis:** found by the A/A test, which is exactly what that test exists for. Resampling
within (arm × cause × band) is what `08-EVALUATION.md` specifies, but a cell holding one
account resamples to itself every time and contributes **zero** variance. On 2,000 accounts
the interval width was right (14.4L against a true null width of 13.9L); on 800 it had
collapsed to 7.4L against a true null of 22L, and the A/A run reported a significant
+₹4.8L lift where the true effect is zero by construction.

**Consequence:** validated by re-randomisation — under `nothing` both arms get identical
treatment, so re-labelling arms is an exact null. Interval width now tracks the true null
distribution (ratio 1.02 at n=800, 1.16 at n=2,000 — erring wide, the safe direction) and
A/A coverage is 57/60 and 58/60 against a nominal 95%.

**If falsified:** the threshold is one constant. If a batch is large enough that every cell
clears 25 on its own, pooling never fires and the estimator is unchanged.

---

## 2026-09-05 — The merchant default is pinned to the primary mandate

**Chose:** `World.primary_mandate()` for the baseline policy; `active_mandate()`, which
searches every rail, is reserved for the agent.

**Basis:** caught by ledger invariant 8 on the first full run — two debits appeared with no
referencing notice. The cause was that the mandate-cancellation hazard revoked the primary
mandate mid-run and `active_mandate()` silently failed over to the account's second rail.
That is *multi-rail repair*, the agent's most differentiated action, being performed by the
do-nothing baseline — which would contaminate the holdout arm and understate every lift.

**Consequence:** the baseline now retries the mandate that failed, and gets `AP53` if it was
cancelled, which is the honest do-nothing behaviour. `notice_window_violations` is 0.

---

## 2026-09-05 — A completed batch refuses a second run

**Chose:** `run_batch` raises `AlreadyRun` if the batch already holds any `EXECUTE` or
`CLOSE` event, rather than resuming.

**Basis:** the world is regenerated from the seed, but the executor's idempotency keys are
rebuilt from the ledger. Resuming would therefore suppress actions the freshly-generated
world has not performed — a suppressed pre-debit notice would leave the following retry
failing its own notice window — and the world and the ledger would silently disagree.
Correct resume needs the world's effects replayed from the event log, which is not built.

**Consequence:** idempotency protects against double-submission *within* a run, which is
what M4 asks for. Crash-recovery is `rr simulate` to a fresh path. The ledger-rebuilt key
set is kept as the safety net that makes the refusal redundant rather than load-bearing.

**If falsified:** if resume is ever needed, replay the ledger's EXECUTE events into the
world before the day loop, rather than relaxing this check.

---

## 2026-09-05 — Arms are read back from the ledger, never recomputed

**Chose:** when `ASSIGN` events already exist, `run_batch` loads the arms from them
instead of calling `assign_arms` again.

**Basis:** `02-LEDGER.md` — "written at ASSIGN and copied onto every later event. Never
derived at read time." Recomputing means a change to the assignment code, the config, or
the holdout fraction could silently move accounts between arms partway through a batch.
That is exactly how a holdout gets corrupted, and it would be invisible in the output.

**Consequence:** the arm is a fact in the log, not a function of current code. A partial
assignment (some accounts assigned, others not) is treated as corruption and refused
rather than topped up.

---

## 2026-09-05 — Settlement observations carry no `ok`

**Chose:** the daily settlement-feed drain writes `OBSERVE` events whose `result` omits
`ok`, so `settled` stays NULL in the index. Exactly one row per cycle — the one written at
CLOSE — carries `ok`.

**Basis:** the scoreboard's headline is `SUM(CASE WHEN settled=1 THEN amount_paise END)`.
Any second settled-carrying row for an account would double its recovered value, and the
error would look like a good result rather than a bug.

**Consequence:** the feed rows are free to be as numerous as the timeline needs. They are
what `days_to_recover` p50/p90 is computed from, and what makes an account's timeline show
when the money actually arrived rather than only that it did.

---

## 2026-09-05 — POL-STOP-001 does not suppress the pre-debit notice

**Chose:** an opt-out denies the contact actions (`SEND_MESSAGE`, `SEND_PAYMENT_LINK`,
`REQUEST_PTP`, `VOICE_CONFIRM_PTP`) but not `SEND_PREDEBIT_NOTICE`.

**Basis:** the same reasoning `05-POLICY-ENGINE.md` gives for exempting the notice from
quiet hours. An opt-out withdraws consent to *recovery outreach*; the pre-debit notice is
a regulatory notification attached to a live mandate, and the customer who wants the
debits themselves to stop revokes the mandate rather than opting out of messages.
Suppressing it would block a lawful debit rather than protect anyone.

**Consequence:** an opted-out account can still be debited on its existing mandate, with
notice, and receives no recovery messaging. In practice the world closes the cycle as
`OPTED_OUT` anyway, so `POL-STOP-005` denies everything a step later — but the rule has to
be right independently of that.

**If falsified:** if an opt-out is held to cover the notice too, add
`SEND_PREDEBIT_NOTICE` to POL-STOP-001's `applies_to`, bump `policy_version`, and re-run.

---

## 2026-09-05 — POL-AFA-002 is enforced as consent freshness

**Chose:** `REREGISTER_MANDATE` is denied unless `consent.afa_authorised_at` is within
`policy.afa_freshness_days` (90). This required one new field on `ConsentState`.

**Basis:** `04-CAUSE-TAXONOMY.md` lists "re-registering without fresh consent" as
explicitly wrong for `MANDATE_REVOKED`. Reading POL-AFA-002 as "the registration flow
happens to include an AFA step" would make it decorative, since our adapter always lands a
new mandate in `PENDING_AFA` and the rule could never deny.

**Consequence:** repairing a mandate onto another rail is a two-step sequence — obtain
consent, then re-register — rather than something the agent can do unilaterally. That is
the demo's best moment (`AP17` NRE repaired onto UPI Autopay) and this is what makes it
legitimate rather than a silent rail switch.

---

## 2026-09-05 — POL-QH-003 is enforced nationally, not regionally

**Chose:** `Calendar` carries national festival and gazetted-holiday dates. Its `regional`
mapping is declared and unused.

**Basis:** `Account` in `01-DOMAIN-MODEL.md` has no region field, and inventing one to
satisfy a rule would be adding domain surface to make a checkbox green.

**Consequence:** POL-QH-003 suppresses contact on national festival dates for every
account. That is the conservative direction — it suppresses more contact than a regional
rule would, never less. Narrowing it needs a region on the account, which is a domain
change, not a policy one.

---

## 2026-09-05 — `run_merchant_default` deleted; the runner is the only baseline

**Chose:** removed the simulator's own driver for the do-nothing policy. `app.runner`
implements it once, through the real compliance gate, and `test_selfcure` now measures
that pipeline.

**Basis:** once M5 put the gate in the runner, the two diverged — the simulator's driver
still minted unconditional ALLOWs. The stop-the-line self-cure number would have described
code the batch does not actually run, and two implementations of the *holdout arm's
behaviour* is precisely how a control group stops meaning anything.

**Consequence:** self-cure measured through the gate is 0.397 on seed 42 (0.372–0.397
across seeds), still inside the 0.30–0.50 band. `provisional_gate` survives only as a
simulator-local fixture for the rail tests, and `tests/test_boundaries.py` pins its user
list at exactly one file.

---

## 2026-09-05 — Dry-run gate events are a marked, verifiable exception

**Chose:** `EventDraft.dry_run` is a boolean field, not a note. A dry-run `GATE` may be
appended before `ASSIGN`, is skipped by the account-ordering invariant, and `verify()`
refuses any `EXECUTE` that leans on a dry-run ALLOW.

**Basis:** `POST /policy/evaluate` mints real `GateDecision` objects. Without the last of
those three, the demo endpoint would be a way to mint executable ALLOWs — the one thing
the whole gate exists to prevent. The ordering invariant exists so nothing *acts* on an
account before its arm is fixed; a hypothetical reads no arm and changes nothing.

**Consequence:** the denial produced on stage is itself in the trail shown thirty seconds
later, and a hypothetical ALLOW is inert by construction rather than by convention.

---

## 2026-09-05 — SQLite `synchronous = NORMAL`

**Chose:** WAL plus `synchronous = NORMAL`, keeping the commit-per-append contract.

**Basis:** measured — 5,000 commits take 11.0s at FULL and 0.3s at NORMAL. A 2,000-account
run writes ~21,000 events, so FULL spent 40 seconds of every run waiting on fsync for a
durability guarantee the ledger does not depend on: WAL+NORMAL still survives an
application crash, and the batch is regenerable from its seed in any case.

**Consequence:** `rr run` on 2,000 accounts went from 57s to 15s, and `rr simulate` from
6.0s to 1.6s, with byte-identical output. Only an OS crash or power loss can lose the last
few commits.

---

## 2026-09-05 — Layer 2 is a hand-written Bayesian update, not a fitted model

**Chose:** the posterior is a prior from the code map multiplied by one likelihood ratio
per observed feature, then normalised. `04-CAUSE-TAXONOMY.md` offers this as an
alternative to multinomial logistic regression and asks which was chosen and why.

**Basis:** three reasons, in order. It needs no train/eval batch orchestration and no
frozen model artefact to version alongside the policy. Every number is a named claim about
the world sitting in `config/default.yaml`, so a denial or a decision can be re-derived by
hand from the ledger's evidence strings — which is what an audit trail is for. And it adds
no dependency.

**Consequence:** `diagnose.evidence` in the config is the model. Changing a likelihood
ratio is a config change, visible in `config_hash` and therefore in the batch record.

---

## 2026-09-05 — The simulator reports imperfect rail codes

**Chose:** added `sim.code_noise = 0.12`. With that probability a rail reports a code
belonging to a different cause than the one that actually occurred, and the true cause is
persisted to `latent_truth.true_cause` for the evaluator to score against.

**Basis:** without it the simulator emits a perfect code every time, a deterministic map is
a *perfect* diagnosis, and Layer 2 has nothing it could possibly add. The premise of the
whole taxonomy section is that these codes are unreliable — sponsor banks populate them
inconsistently and NPCI's own rewrite added 20 codes, revised 33 and removed 22. A
simulator that emits them perfectly makes the section decorative and the claim untestable.

**Consequence:** the diagnosis story is now falsifiable, and it was immediately falsified —
see the next entry. Mechanically nothing changed: the code is what the bank *says*, not
what happened, so self-cure and every recovery number are unaffected.

---

## 2026-09-05 — Layer 2 adds nothing measurable; Layer 1 is what ships

**Measured**, on 4,000 accounts at 12% code noise:

| | accuracy vs true cause |
|---|---|
| Layer 1 — code map alone | 0.884 |
| Layer 2 — posterior | 0.884 |

And at the decision level, which `04-CAUSE-TAXONOMY.md` says is the one that matters
("score decisions, not labels"): of 929 accounts where retrying is futile, both offer
`RETRY_DEBIT` for 13 of them. Of 3,071 where retrying is sensible, both offer it for 2,396.
Identical to the account.

**Why, and why it is not a bug:** the reported code is right ~88% of the time, so the 0.75
prior on it is if anything *under*-confident. For a history feature to overturn the argmax
it would need a likelihood ratio above ~21; the ratios in config are 1.4–2.5. Lowering the
prior until the posterior started disagreeing would be tuning to manufacture a difference,
and it would push the estimate away from the code's actual reliability — making diagnosis
worse, not better.

**Chose:** ship Layer 1 as the diagnosis, exactly as `04-CAUSE-TAXONOMY.md` counsels
("Ship the Layer 1 map even if Layer 2 never lands"). Keep the posterior, because the
planner takes a posterior-weighted argmax over per-cause Q values (`06-PLANNER.md`) and
needs a distribution rather than a point estimate — its value there is measurable at M8
even though its value *here* is zero.

**Consequence:** `test_layer2_is_never_worse_than_layer1` pins the direction rather than
the number: a change to the priors or ratios must not degrade the deterministic floor.
This is the same shape as the LLM ablation the project plans for, arrived at earlier and
for the same reason — having run the comparison is the point, whatever it says.

---

## 2026-09-05 — A prohibition under any plausible cause wins

**Chose:** the eligible set is the union of what every plausible cause allows, minus the
union of what any of them explicitly forbids. "Plausible" is posterior mass at or above
`diagnose.plausible_threshold` (0.15).

**Basis:** encoding only the "eligible actions" column of the matrix would let a
20%-probable structural defect be papered over by an action eligible for the 80% cause. If
there is a real chance the mandate is dead, the retry cannot work and its cost and its
notice are both wasted.

**Consequence:** conservative in the safe direction — the agent declines actions it might
have got away with, rather than spending attempts on mandates that cannot clear.
`WAIT` and `CLOSE` are appended universally, so the set is never empty and the economic
stopping rule is always reachable.

---

## 2026-09-05 — `REREGISTER_MANDATE` is withheld when there is nowhere to move

**Chose:** the action is removed from the eligible set when every non-`PAYMENT_LINK` rail
already carries a broken or existing mandate for the account.

**Basis:** the planner prices what it is offered. An action that cannot physically happen
would be scored, possibly chosen, and then fail at execution — putting a phantom in the
audit trail and wasting a decision.

**Consequence:** `alt_rail_available` in `PlanState` is a fact about the eligible set
rather than a separate lookup the planner has to remember to make.

---

## 2026-09-05 — One RNG stream per account, not one per batch

**Chose:** every stochastic draw that affects an account — its balance dynamics, its
hazards, its rail outcomes, its reported codes — comes from a generator seeded on
`(batch seed, account index)`.

**Basis:** found at M7 and it is a serious one. With a single shared stream, a treatment
policy that acts more often consumes more draws, which shifts every subsequent draw for
**holdout** accounts too. Measured: running `nothing`, `fixed` and `oracle` over the same
world gave holdout recovery rates of 41.1%, 31.8% and 38.3% — three different numbers for
a control group that was treated identically in all three. A control group whose outcome
depends on what the treatment arm did is not a control group, and every incremental figure
computed against it is contaminated.

**Consequence:** the holdout now recovers ₹7,55,754.10 in all three runs, to the paisa.
`test_holdout_is_invariant_to_the_treatment_policy` asserts exactly that, and it is the
single most important test in the suite — it is the machine-checkable form of "the arms
are independent". Comparing two policies is now a *paired* comparison on the same
treatment accounts, which is far tighter than differencing two noisy incrementals.

**If falsified:** if a future hazard is added, it must draw from `world.rng(account_id)`.
Adding one that reaches for a shared stream silently re-couples the arms.

---

## 2026-09-05 — A regulatory notice does not spend the discretionary contact budget

**Chose:** `CycleState` tracks `contacts` (everything the customer receives) and
`recovery_contacts` (discretionary outreach only). POL-FREQ-001 and POL-FREQ-002 count the
second; the harm scoreboard reports the first.

**Basis:** caught by the M7 acceptance test. The `fixed` baseline executed **zero** of its
messages — all 1,155 attempts denied by POL-FREQ-002, because the day-2 SMS collided with
that day's mandatory pre-debit notice. Counting the notice against the recovery-contact cap
means complying with the e-mandate framework silently spends your allowance under the
conduct norms, which cannot be the intent of either. It is also inconsistent with
POL-QH-001, which already exempts the notice on exactly this reasoning: the frequency rules
govern *recovery* contact.

**Consequence:** the split is deliberate and both halves are reported. The gate caps what
the regulation caps; the scoreboard still counts the notice as a contact, because from the
customer's side it is one. `fixed` now sends 3,524 messages instead of 0.

**If falsified:** if the conduct norms are read to cap total customer touches, point
POL-FREQ-001/002 at `contacts` instead of `recovery_contacts`, bump `policy_version` and
re-run. Nothing else changes.

---

## 2026-09-05 — The oracle lives in `app/sim/` and is never a reported result

**Chose:** `app/sim/oracle.py`, imported lazily inside `app.policies.build()` so that
importing the policy module does not pull the simulator into an agent code path.

**Basis:** the oracle reads latent balance by definition. Putting it anywhere else would
either break the module boundary or — worse — quietly not break it, because the boundary
test only inspects the agent packages.

**Consequence:** measured at 6,000 accounts, the oracle converts **2,733 rail recoveries
from 7,784 debits**, against the merchant default's 1,829 from 6,831: +49% recoveries for
+14% attempts. That gap is what knowing the customer's balance is worth, and `agent /
oracle` at M8 is the share of it the planner captures.

---

## 2026-09-05 — M7 result: `fixed` beats `nothing`, but barely, and expensively

**Measured** at 6,000 accounts, treatment arm, paired on the same world and arm split:

| policy | recovery | debits | messages | opt-outs |
|---|---|---|---|---|
| `nothing` | 39.0% | 6,831 | 0 | 34 |
| `fixed` | 39.9% | 9,831 | 3,524 | 95 |
| `oracle` | 49.8% | 7,784 | 0 | 52 |

The ordering the build plan requires holds. What it costs is the finding: the incumbent
buys **+0.9pp of recovery for 44% more debit attempts, 3,524 messages, and 2.8x the
opt-outs**. The calendar is spending attempts on days when the money is not there, and
paying for it in customer goodwill.

The oracle recovers +10.8pp over the merchant default from only 14% more attempts — and
with *fewer* self-pay recoveries (681 against 753), because it collects by rail before the
customer gets round to it. Nearly all of the achievable value is in *timing*, not volume,
which is the thesis stated as a measurement rather than an argument.

---

## 2026-09-05 — M8 result: the planner targets far better and recovers no more

**Measured** on 4,000 accounts, seed 42, λ=0.5:

| policy | debits | successes | **hit rate** | treatment recovery |
|---|---|---|---|---|
| `nothing` | 1,121 | 299 | 26.7% | 38.1% |
| `agent` | 326 | 237 | **72.7%** | 37.8% |
| `oracle` | 1,314 | 456 | 34.7% | 47.6% |

The planner's attempts are **2.7x better targeted** than the merchant default's, at a
third of the volume and 15% lower cost. Its headline incremental is ₹11,056, 95% CI
[−₹9.98L, +₹9.83L] — indistinguishable from zero.

**Where the value is and is not**, per cause:

| cause | n | incremental |
|---|---|---|
| `INSUFFICIENT_FUNDS` | 1,549 | **+12.6%** (+₹8.16L) |
| `MANDATE_INVALID` | 362 | **+5.5%** (+₹0.70L) |
| `TRANSIENT_INFRA` | 895 | −2.7% |
| `AUTH_ARTEFACT` | 435 | −7.4% |
| `ACCOUNT_TERMINAL` | 209 | −11.5% |
| `LIMIT_EXCEEDED` | 291 | −21.1% |
| `MANDATE_REVOKED` | 259 | −24.0% |

The thesis holds exactly where it was claimed to: on the timing bucket, which is the
largest segment, waiting for the customer's inflow and committing a notice a day ahead is
worth **+12.6%**. Mandate repair is worth **+5.5%**. Those two are the whole argument, and
both are positive.

Every negative segment is one where the agent **stops** (1,562 accounts close as
`EV_BELOW_THRESHOLD`) and the merchant default keeps blindly retrying. And blind retrying
sometimes works — *because the reported code was wrong*. At 12% code noise an account
labelled `MANDATE_REVOKED` that is really `INSUFFICIENT_FUNDS` recovers from a dumb retry
and is closed by a smart one.

**So the finding is: the stopping rule is only as good as the diagnosis.** The agent is
paying the full price of acting on its beliefs while the baseline pays nothing for having
none. That is a real property of confident systems and it is worth putting on a slide.

**Not tuned.** `10-BUILD-PLAN.md` is explicit: "If the interval straddles zero, do not tune
until it does not. Report it honestly, then look at the per-cause cuts to find out *where*
the agent is failing to add value. That investigation is a better result than a tuned
number." The per-cause cut above is that investigation, and it names the fix — the stopping
rule needs to price the chance that the diagnosis is wrong, not just the value of acting on
it being right. That is a change to `choose()`, not to a constant.

---

## 2026-09-05 — Closing the workflow does not stop the customer paying

**Chose:** `CycleState.open` (the agent may act) and `CycleState.unsettled` (the money has
not arrived) are separate. Self-pay runs for every unsettled cycle, closed or not.

**Basis:** they were the same predicate, so an account the agent closed was removed from
the self-pay hazard entirely. The agent was being charged for stopping — by deleting the
free self-cure it was right to stop competing with. With 1,562 accounts closed early that
was most of the measured shortfall, and it was an artefact of the simulator rather than a
property of the policy.

**Consequence:** stopping is now free in exactly the way it should be. The agent declines
to spend; the customer keeps their own options.

---

## 2026-09-05 — The eligible set mirrors what the gate will permit

**Chose:** `eligible_actions` also removes `RETRY_DEBIT`/`SEND_PREDEBIT_NOTICE` above the
AFA-free ceiling, and `REREGISTER_MANDATE` without fresh customer authorisation — the two
conditions POL-AFA-001 and POL-AFA-002 refuse on.

**Basis:** measured. Before this the agent proposed 197 retries of which 111 were denied,
and 51 re-registrations of which **all 51** were denied. The planner was pricing actions
that could never happen, then watching the gate refuse them: a wasted decision, a phantom
in the audit trail, and an eligible set that — in the words of its own module docstring —
"contains lies".

**Consequence:** the eligible set is now the honest input to the planner. `POL-AFA-002`
denials fell from 51 to 1 on a 4,000-account batch.

---

## 2026-09-05 — Only MANDATE_INVALID unlocks retries after repair

**Chose:** `MANDATE_HEALTH_CAUSES = {MANDATE_INVALID}`, having briefly included
`MANDATE_REVOKED` and `ACCOUNT_TERMINAL`.

**Basis:** the `mandate_ok` state dimension exists so the MDP can see that repairing a dead
mandate unlocks the retries that make repair worth paying for. But a revoked mandate needs
fresh consent and a closed account has nothing behind it, so neither can ever reach
`mandate_ok = True` — and pricing retries in that unreachable state let posterior mass on
"this account is closed" quietly support debiting it anyway. Caught by
`test_choice_weights_by_the_posterior`, which found the weighted Q for a debit identical
whether the posterior was certain or half on `ACCOUNT_TERMINAL`.

---

## 2026-09-05 — A daily discount, and why WAIT needed one

**Chose:** `planner.daily_discount = 0.995` on every continuation value.

**Basis:** `WAIT` costs nothing, so without a time preference it *ties* every action it
could postpone, and argmax deferred indefinitely — the agent waited until the horizon ran
out. A discount is the standard, principled expression of the fact the scoreboard already
measures: money on day 4 is worth more than the same money on day 27.

**Consequence:** ties break toward acting, and `SEND_PREDEBIT_NOTICE` now beats `WAIT` in
exactly the states where committing a day in advance is the right play.

---

## 2026-09-05 — `lambda_harm` is excluded from the world hash

**Chose:** `Config.world_hash` covers everything except `run.lambda_harm`; the drift check
uses it, while the batch record still stores the full `config_hash`.

**Basis:** λ changes what the *agent chooses*, never what the customers do. Including it
meant every point on the λ frontier looked like config drift and the sweep could not be run
against a batch at all.

---

## 2026-09-05 — A denominator must not exceed one

**Fixed:** `rate_per_retried` credited *all* recovered value against a denominator
restricted to accounts the agent attempted — and reported **132.6%**. The numerator and the
denominator have to describe the same accounts. Now 75.7%, and
`test_no_denominator_can_exceed_one` pins it.

**Worth noting:** this is the flattering denominator, the one `08-EVALUATION.md` warns
vendors quote. It is the one that broke, and publishing all three next to each other is
what made it obvious.

---

## 2026-09-05 — The stopping rule now prices the chance the diagnosis is wrong

The M8 per-cause cut showed every negative segment was one where the agent **stopped**
and the merchant default kept blindly retrying — and blind retrying sometimes worked,
because the reported code was wrong. Three separate defects were doing that, and all
three were the same mistake: treating a noisy diagnosis as certain.

**1. The account's own records were not evidence.** The posterior read the rail's decline
code and the account's history, but never the merchant's own mandate registry. That
registry is written by registration and revocation events, not by a per-transaction
decline — so when the two disagree, they are two independent witnesses and the code is
the weaker one. `contradictions()` now fires on three specific disagreements: a code
claiming the mandate is dead while we hold a live one, a code claiming over-limit while
the amount sits inside the registered cap, and a code claiming an auth artefact against
an ACTIVE mandate. On `AP53` against a live mandate, `MANDATE_REVOKED` falls from 0.71 to
0.25 and `INSUFFICIENT_FUNDS` rises to 0.22.

*This is also what finally makes Layer 2 earn its place.* M6 measured it adding exactly
0.000 over the code map, because no feature in the set was strong enough to overturn an
88%-reliable prior. A contradiction from an independent source is, and it is the only one.

**2. The eligible set vetoed on any plausible cause, however weak the belief.** That was
the right call for prohibitions that protect a *person* — pestering someone whose bank
timed out — and miscalibrated for prohibitions that only protect a *budget*. A wasted
retry costs ₹2.50, and refusing it on a 25% structural claim forfeits a 75% chance at the
whole cycle. `WRONG` is now split:

- `WRONG_HARM` — inappropriate to do to a customer. Vetoed on any plausible cause.
- `WRONG_FUTILE` — simply will not work. Vetoed only at near-certainty (0.8).

The threshold is high deliberately. The planner already does the probabilistic reasoning
properly: it solves per cause, prices a futile action at −∞ under the causes that forbid
it, and the posterior-weighted Q therefore counts only the mass where the action can
succeed. A veto in the eligible set is a cruder second mechanism doing the same job, and
its only real use is sparing the planner an all-but-certainly pointless decision.

**3. The rule stopped on the value of *acting today*, not on the value of the state.**
Closing is irreversible and `WAIT` is free, so an agent that closes whenever nothing is
worth doing right now discards everything it could have done later — including on the
share of the posterior where its diagnosis is wrong. It now closes when the
posterior-weighted `V(s)` is at or below `planner.close_threshold_paise`, which is the
value of the state under optimal play to the horizon, across every cause.

**Measured**, 4,000 accounts, seed 42, λ=0.5, against the same run before the fix:

| | before | after |
|---|---|---|
| incremental recovered | ₹11,057 (0.1%) | **₹2,73,118 (1.7%)** |
| closed `EV_BELOW_THRESHOLD` | 1,562 | 1,482 |
| `MANDATE_REVOKED` | −25.4% | **−14.3%** |
| `MANDATE_INVALID` | +5.5% | **+10.1%** |
| `ACCOUNT_TERMINAL` | −11.5% | **−4.7%** |
| `INSUFFICIENT_FUNDS` | +12.6% | +12.6% |

The interval still straddles zero (95% CI [−₹7.28L, +₹12.48L]) and is not claimed
otherwise.

**What remains, and it is a different mechanism.** `AUTH_ARTEFACT` (−7.4%) and
`LIMIT_EXCEEDED` (−19.2%) did not move, and the reason is not the diagnosis. Those
accounts genuinely cannot be debited — a `PENDING_AFA` mandate has no live mandate to
debit and an over-cap amount is refused by POL-AFA-001 — so the agent correctly declines
to attempt. But **declining to attempt means declining to send the pre-debit notice, and
the notice itself lifts self-pay.** The merchant default sends two notices to every
account whatever the diagnosis, and collects the self-cure that follows.

That is exactly the effect `08-EVALUATION.md` names as notice-attributable lift: "some of
what a naive system credits to clever messaging is caused by the notification the
regulator forced it to send." Here it shows up inverted — as a cost the *agent* pays for
being selective. It is a real finding, it is now isolated to two segments, and closing it
is a question about whether a notice has standalone value rather than another correction
to the stopping rule.

---

## 2026-09-05 — M9: the promise machine is built, correct, and never chosen

The state machine works. `OPEN → KEPT | BROKEN | PARTIAL | LAPSED` resolves automatically
on `promised_date + grace_days` against settlement data, an open promise suppresses
outreach through both POL-PTP-001 and the eligible-set overlay, a kept promise restores
the contact budget, and a broken one decays that account's trust in its next promise.
All four transitions are exercised end to end against real settlement in
`test_all_four_transitions_fire_in_the_world`.

**The planner never selects `REQUEST_PTP`.** Measured, and it is a structural result
rather than a tuning artefact: `SEND_PAYMENT_LINK` has both a higher recovery lift (0.14
against 0.10) *and* a lower harm weight (0.4 against 0.6), and it is eligible under every
cause that permits a promise. Asking for a promise is therefore dominated in every state,
at every λ — at λ=0 the harm terms vanish and the lift still decides it.

**Why the domination is arguably wrong, and what would fix it.** The MDP prices
`REQUEST_PTP` as a one-shot recovery lift, which misses the two things a promise actually
buys: it *suppresses* subsequent outreach, so it saves future harm the planner never
credits it for, and it reveals a date the customer chose — their own account of their
inflow phase, which is information no return code carries. Pricing either needs a
`ptp_open` dimension in `PlanState`, not a bigger lift constant. Raising `lift.REQUEST_PTP`
until the agent picked it would be manufacturing the answer.

Stated plainly, because it is the honest version: under the declared numbers the agent
prefers to hand someone a way to pay over asking them to commit to a date, and that is a
defensible preference. `test_request_ptp_is_dominated_by_the_payment_link` pins it, so if
the pricing or the state space changes, the finding is re-examined rather than forgotten.

---

## 2026-09-05 — A promise that outlives the horizon lapses, it does not break

**Chose:** the run's close loop resolves any still-open promise as `LAPSED` before writing
the cycle's terminal state.

**Basis:** found by the acceptance test, which could not make `LAPSED` fire at all. A
promise is only resolved once it comes due, so one made for a date beyond the measurement
horizon was never resolved by anything — it simply stayed `OPEN` forever. Recording it as
`BROKEN` instead would be worse: the customer never reached the date they named, so
punishing their future trust for our horizon is both unfair and a corrupted signal.

**Consequence:** `next_confidence` leaves trust untouched on `LAPSED`, which is the only
one of the four statuses that says nothing about the customer.

---

## 2026-09-05 — Partial payments exist only against a promise

**Chose:** `CycleState.paid_paise` accumulates, and `settled` still requires the full
amount. Partial payment happens only when a promise comes due and the money is short.

**Basis:** `PARTIAL` is one of the four statuses the state machine must reach, so it has
to be reachable by something. Confining it to promises keeps general self-pay behaving
exactly as before — so the calibrated self-cure band is untouched — while giving the
status a real path. It is also the realistic case: someone who committed to an amount and
came up short pays what they have.

**Consequence:** a half-paid cycle is not a recovery. `paid_paise` is reported separately
rather than folded into the headline, because folding it in would let partial money
inflate a recovery rate.

---

## 2026-09-05 — The false-positive distress rate is config, not a literal

**Fixed:** `sim.distress_phrase_p_other = 0.005`, which had been a hardcoded `0.01` — a
plain violation of CLAUDE.md rule 8, and in the one place where a silent constant would
have quietly set the precision of the hardship detector that M11 is meant to report.

Non-zero on purpose: a detector with no false positives is a detector nobody has measured.
The acceptance test asserts the *rate* difference between hardship and non-hardship
accounts, which is what the model guarantees; precision also depends on how rare hardship
is, and belongs in the evaluator's diagnostics scored against `latent_truth`.

---

## 2026-09-05 — `rules` is a heuristic, not the planner wearing a hat

**Chose:** `RulesProposer` picks from the eligible set by a fixed per-cause priority
order, using no Q values and no MDP.

**Basis:** `07-PROPOSER-GROQ.md` describes the rules proposer as using "the cause →
eligible matrix plus the planner's Q values" — but that is exactly what
`planner-argmax` is, so arms A and B of the three-way ablation would have been the same
thing measured twice. As built, the ablation answers two separate questions:

    rules -> planner-argmax    what the MDP adds over a sensible heuristic
    planner-argmax -> groq     what the LLM adds over the MDP

**Consequence:** the deterministic floor is genuinely a floor. It is also still the
control arm the repo falls back to with no API key, exactly as the doc requires.

---

## 2026-09-05 — M10 result: the MDP is worth ₹6.71 lakh over a heuristic

**Measured**, 1,200 accounts, seed 42, λ=0.5, identical world and arm assignment:

| arm | recovery | incremental | 95% CI | cost | contacts |
|---|---|---|---|---|---|
| `rules` | 21.8% | **−₹4,50,213** | [−₹10.40L, +₹0.88L] | ₹1,197 | 3,942 |
| `planner-argmax` | 36.0% | **+₹2,20,545** | [−₹3.68L, +₹7.43L] | ₹955 | **863** |
| `groq` | — | unavailable, no API key | | | |

**The MDP adds ₹6.71 lakh over the heuristic, with 4.6x fewer customer contacts.** This is
the first unambiguously positive result in the project, and it is the one that matters:
the planner is the algorithmic content, and this is the measurement that says the
arithmetic earns its place.

The heuristic *destroys* value — it recovers 21.8% against the holdout's ~36% — because it
acts on every account every day it can. 3,942 contacts against 863 drives annoyance, which
suppresses self-pay and raises opt-outs, and the extra recoveries do not cover it. That is
the same shape as the M7 finding about the incumbent's fixed schedule, arrived at from the
other direction: volume is not the lever, timing is.

The heuristic agreed with the planner's own pick **1.6%** of the time. The two are doing
genuinely different things, which is what makes the comparison worth anything.

**The Groq arm is unverified.** There is no API key in this environment and no network, so
the live path has never executed. Its logic — retry-once, schema validation, eligible-set
enforcement, caching, statistics — is tested against an injected transport, and the model
IDs and strict-mode support are as documented rather than as observed. `12-GLOSSARY.md` V7
already flags that model IDs churn. Do not present a Groq number until it has been run.

---

## 2026-09-05 — The Groq client uses the standard library

**Chose:** one `urllib.request` POST rather than `httpx` or the OpenAI SDK.

**Basis:** a single JSON call does not justify a dependency, and the repo has to run
end-to-end without a key anyway — adding a package for a path that is off by default means
the whole project stops installing where that package will not.

**Consequence:** the transport is a plain callable and therefore injectable, which is what
lets the retry, validation and caching behaviour be tested without a key or a network.

---

## 2026-09-05 — The ablation reports an unavailable arm rather than substituting one

**Chose:** with no key, the `groq` row is printed as unavailable with the reason. It is
*not* silently filled in with the rules proposer.

**Basis:** the doc says a run with no key is labelled `proposer=rules`, which is right for
a *batch*. For an ablation it would be actively misleading: a comparison in which one arm
has been quietly replaced by another arm is worse than a comparison with a gap in it.

**Consequence:** `rr ablate` always prints a table, and it always says which arms actually
ran. `test_ablation_runs_without_a_key` asserts the gap is reported rather than papered.

---

## 2026-09-05 — Hardship is a conduct rule, not an expected-value calculation

**Chose:** when the hardship overlay fires, `choose()` returns `OFFER_ACCOMMODATION` with
terminal `HARDSHIP` directly, without pricing anything.

**Basis:** found because the detector flagged accounts and *none* of them exited as
`HARDSHIP`. The overlay replaces the eligible set with
`{OFFER_ACCOMMODATION, ESCALATE_HUMAN, CLOSE}`, but none of those appear in any cause's
`ALLOWED` set, so the MDP priced them at −∞ and the agent fell through to a plain `CLOSE`
with `EV_BELOW_THRESHOLD`.

Fixing that by adding them to the matrix would have been the wrong repair. Pricing this
decision means asking whether it is *profitable* to stop chasing someone who cannot pay,
and it is not meant to be. RBI's draft norms expect a lender to identify borrowers in
repayment difficulty and offer guidance — that is a rule about conduct, and it belongs in
the same category as "distress language ends the call", which `05-POLICY-ENGINE.md`
already calls a rule rather than a model output.

**Consequence:** hardship exits appear on the scoreboard (8.8 per 1,000 treatment
accounts), and the proposer is skipped entirely under the overlay — there is nothing for
an LLM to choose between.

---

## 2026-09-05 — The hardship weights were an accident of round numbers

**Fixed:** `repeated_insufficient_funds` 0.30 → 0.35 and `distress_language` 0.55 → 0.65.

**Basis:** the two purely passive signals summed to exactly 0.60 against a strictly-greater
threshold of 0.6, so the canonical hardship picture — repeated shortfall plus a second
merchant failing in the same window — could never fire. That was not a judgement about
evidence; it was three round numbers landing on a line.

The weights are now set so that specific *combinations* clear the threshold, which is what
the evidence actually looks like: distress alone (0.65), or the passive pair (0.65), but no
single passive signal on its own. The reasoning is written into `config/default.yaml` above
the weights, because the next person to change one needs to see what it was balanced
against.

---

## 2026-09-05 — M11 result: the detector is precise and incomplete, for a traceable reason

**Measured** on 2,500 accounts against `latent_truth`:

| | |
|---|---|
| precision | **0.81** |
| recall | **0.10** |
| F1 | 0.18 |
| base rate | 7.0% |
| | 17 found, 4 wrongly flagged, 151 missed |

It is right about four out of five accounts it flags, and it finds one in ten of the
accounts it should. The precision is where it should be — when you stop chasing someone
you want to be right — but the recall is poor, and the reason is a chain worth following
rather than a number worth tuning:

**M10** measured `REQUEST_PTP` as strictly dominated by `SEND_PAYMENT_LINK`, so the planner
never asks for a promise. **M9** built a promise machine that is therefore never exercised
in a real run. **M11** has four hardship signals, and *two of them* — distress language and
a broken promise — can only be observed after a `REQUEST_PTP`. The detector is running on
the passive pair alone, and 151 people in genuine hardship keep getting chased because of
a pricing decision three milestones upstream.

That is the most useful thing this milestone produced. It is also the strongest argument
for pricing a promise properly — the `ptp_open` dimension named at M9 — since the value of
asking is not only the money it collects but the people it lets you stop asking.

---

## 2026-09-05 — M11 result: the frontier, and what it says

**Measured**, 1,500 accounts, seed 42, identical world and holdout at every point:

| λ | recovery | contacts | attempts | opt-outs/1k | stopped early |
|---|---|---|---|---|---|
| 0.0 | 39.4% | 2,031 | 785 | 9.2 | 378 |
| 0.25 | 38.7% | 1,157 | 548 | 5.0 | 539 |
| 0.5 | 39.1% | 1,050 | 568 | 5.0 | 537 |
| 1.0 | 40.8% | 909 | 593 | 3.3 | 530 |
| 2.0 | 39.9% | 642 | 430 | 0.8 | 663 |

Contacts fall monotonically, which is the acceptance criterion: harm is priced rather than
mentioned. Opt-outs fall 11-fold across the range.

The striking part is the first column. **Recovery does not move** — 39.4% at λ=0 against
39.9% at λ=2 — while contacts fall 68% and opt-outs fall 91%. On this batch the agent at
λ=0 is spending three times the customer contact to collect the same money. That is a
sharper statement of the project's thesis than the incremental figure has managed so far,
and it is the same finding as M7's incumbent schedule and M10's heuristic proposer, from a
third direction: volume is not the lever.

`render()` refuses to look convincing if the monotonicity ever breaks — it prints a note
saying the plot should not be shown until that is understood, because a frontier that does
not bend is decoration.

---

## 2026-09-05 — Razorpay test mode behind the same `RailAdapter`

**Chose:** a live adapter in `app/rails/razorpay.py`, selected by `rr live`, alongside the
simulator rather than replacing it. `RailAdapter`, `Settlement` and — crucially — the gate
check moved to `app/rails/base.py` so both adapters share one implementation.

**Basis:** the README already claimed "swapping the simulator for a sandbox is a one-file
change behind `RailAdapter`". That was a claim about a seam nobody had crossed. Crossing it
turns it into a demonstration, and the track asks for measured money *and* an audit trail —
this gives the trail real provider ids that can be checked against a system we do not
control.

`require_gate` is one function used by both, not two implementations that happen to agree:
`test_both_adapters_share_one_gate_check` asserts identity. If the live rail could be
reached ungated, nothing the simulated run proves would transfer to it.

**Consequence, stated precisely because the split matters:**

- **Simulated, at scale** — 2,000 accounts, randomised holdout, confidence intervals. Every
  measured number in this repo comes from here. You cannot randomise a control group
  against a live provider, and you cannot authorise two thousand mandates in a browser.
- **Real, in a slice** — `rr live` drives a handful of at-risk accounts through Razorpay
  test mode. Real payment link ids, real URLs, real error codes, written to the same
  ledger. Verified working: four `plink_*` objects created, `rr verify` clean.

The live slice writes `assignment:live_slice, randomised:false` into its ASSIGN evidence,
because those accounts were *chosen*. They are excluded from the measured comparison for
exactly that reason, and the ledger says so rather than leaving it to a footnote.

---

## 2026-09-05 — The ledger refused the live integration three times, correctly

Worth recording, because each refusal was the invariant doing its job against its own
author:

1. **`an EXECUTE is marked dry_run`** — the live executions were tagged `dry_run=True`,
   copied from the `/policy/evaluate` path. But real Razorpay objects had been created. The
   verifier's rule that a hypothetical must never reach a rail caught it immediately.
2. **`cyc_00000004 is closed`** — the first attempt ran against a completed batch. Nothing
   may follow a CLOSE. A live slice belongs on accounts still at risk, so `rr live` now
   refuses a finished batch with the command to make a fresh one.
3. **`GATE before ASSIGN`** — nothing may touch an account before its arm is written. The
   honest fix was not to bypass it but to write the ASSIGN: an account being driven live
   *is* in treatment.

Three attempts to integrate a real provider, three refusals, no bad data written. That is
the ledger being worth having.

---

## 2026-09-05 — An unrecognised provider code was a crash, not a fallback

**Fixed:** `_razorpay_code` mapped an unfamiliar reason to `CauseClass.UNKNOWN` and then
called `code_for(rail, UNKNOWN)`, which has no answer and raises. The first unfamiliar
reason Razorpay returned would have taken the run down — on precisely the scenario
`04-CAUSE-TAXONOMY.md` exists to survive.

It now returns the reason verbatim, prefixed `RZP:`, which reads as `UNKNOWN`, counts as
unmapped, and puts taxonomy drift on the scoreboard instead of in a traceback.

**Also:** provider reason strings live in `config/codemap.yaml` under `providers.razorpay`,
not in a match statement. A provider renaming a code must never require a code change —
that is the same reasoning that put Layer 1 in config, applied to the layer above it.

---

## 2026-09-05 — Groq: three real defects between "the key works" and "the arm runs"

The Groq path had never executed. With a key it took three fixes, and the third nearly
produced a false finding.

1. **Cloudflare 403.** `urllib`'s default `Python-urllib/3.x` user agent is rejected
   outright — error 1010, *identically with and without a valid key*, so it never reaches
   Groq's auth. Identifying the client properly fixes it.
2. **The strict schema was invalid.** `maxLength`, `maxItems`, `minimum` and `maximum` are
   not in the JSON Schema subset strict mode accepts, and including them fails the whole
   request with an empty `failed_generation` rather than being ignored. Those bounds moved
   into `parse()`, which is where they belonged: a limit the caller enforces holds whether
   or not the provider honours it. `max_tokens` also went 300 → 800, because gpt-oss
   reasons before answering and 300 truncated it to an empty completion.
3. **Rate limiting looked exactly like "the LLM adds nothing".** Unthrottled, a 120-account
   run produced **232 HTTP 429s out of 233 calls**. Every one fell through to the planner,
   which is the designed behaviour — so the ablation ran to completion and reported the
   groq arm as *byte-identical* to `planner-argmax`, with a tidy "the LLM over the MDP:
   ₹0.00" and an interval-overlap note. It was measuring the rate limiter.

   Two things now prevent that reading. Failures are categorised by kind, so a run where
   the model declined and a run where the transport was refused no longer look the same.
   And the interval is derived rather than guessed: `x-ratelimit-limit-tokens: 8000` per
   minute ÷ 932 tokens per measured call = ~8.6 calls/min, one every ~7s. Guessing twice
   (2.1s still gave 72% 429s) is what made it worth measuring.

**Also:** nothing read `.env`, despite the README instructing you to put your key there.
`app/domain/env.py` is eight lines of standard library; the real environment still wins
over the file.

**Still unmeasured:** a complete three-arm ablation with a working Groq arm. At ~7s per
call and a 1,000-request daily cap, one is roughly half an hour. The number that exists
today is not one.

---

## 2026-09-05 — the UI found four bugs that the tests could not

Building `app/api/ui.html` was supposed to be presentation. It was the cheapest bug
hunt of the project, because a page renders every number next to every other number and
a person notices what an assertion does not.

1. **`PENDING_AFA` never became `ACTIVE`.** The timeline panel showed `acc_00003535`
   executing `REREGISTER_MANDATE` on day 9 and again on day 10, then closing on
   `EV_BELOW_THRESHOLD`. Nothing in the simulator ever completed an authorisation, so
   repair could not pay off — while the planner priced it at a 55% completion chance.
   `World.resolve_pending_afa` now completes them at a rate scaled by responsiveness,
   and `eligible_actions` drops `REREGISTER_MANDATE` while one is outstanding. Measured
   completion came out at 42%, so `planner.reregister_afa_completion` moved 0.55 → 0.45.
   Effect on the batch: five of seven causes positive, up from two.

2. **`/policy/evaluate` answered every question with "already closed".** It rebuilt the
   account's compliance context from the *end* of the batch, so asking what the gate
   would have said at 19:30 on day 10 was answered with a closure that happened on day
   15 — `POL-STOP-005`, every time, for every account, and the quiet-hours rule the
   panel exists to demonstrate could never fire. State is now rebuilt as of the
   timestamp asked about: contacts before it, closure only if it had happened. The four
   rule families now each demonstrate: 19:30 → `POL-QH-001`, voice on a non-consented
   channel → `POL-CONSENT-001`, a debit with no notice → `POL-NOTICE-001`, an SMS in
   hours → `ALLOW`.

3. **`onclick="evaluate()"` called `document.evaluate`.** Inline handlers scope-chain
   through `document`, so the button was calling XPath and throwing `2 arguments
   required`. Renamed to `runGate`.

4. **`rr simulate` crashed printing its own success message.** A Windows console is
   cp1252 and cannot encode `₹`. `app.cli.main` now reconfigures stdout and stderr to
   UTF-8 once, rather than degrading the money format everywhere.

And one that was hiding in plain sight: `app/live.py` had
`now = datetime.now(IST) if False else datetime(2026, 9, 10, 11, 0, tzinfo=IST)`. The
dead branch was enough to fail the CLAUDE.md rule-6 boundary test. The live slice now
builds a `Clock` from the batch's own earliest due date — the compliance clock must be
the one the audit trail records, or a demo run at 02:00 local silently becomes a
quiet-hours violation.

**Not a bug, though it reads like one:** treatment recovers at a median of 13 days
against the holdout's 3. The holdout retries on a calendar (days +3 and +7) and catches
the easy self-cures early; the agent waits for the inflow and retries once, at a 72.7%
hit rate against 26.7%. Slower and cheaper, on purpose.

---

## 2026-09-05 — the Groq ablation, and the four ways it was lying

`rr ablate --accounts 600 --seed 42` on `openai/gpt-oss-20b`. The result is below, but
the result is the least interesting part: getting to a number that meant anything took
four fixes, and every one of them was a case of the harness reporting the *provider*
and calling it the model.

**1. The daily limit is the binding constraint, not the per-minute one.** The 7.5s
throttle was derived from `x-ratelimit-limit-tokens: 8000` per minute. That was the
wrong limit. The real wall is `tokens per day (TPD): 200,000`, and at prompt 729 +
`max_tokens` 800 = **1,529 reserved tokens per call** it allows ~130 decisions a day.
The limiter reserves `max_tokens`, not what the reply actually costs — so the cap you
set is the cap you are billed against for rate-limit purposes.

`reasoning_effort: "low"` cuts it. Measured on the live API: default effort spends 427
reasoning tokens of a 556-token completion; low effort spends 150 of 297. With
`max_tokens: 400` the reservation drops to 1,081 and the budget buys ~185 decisions.

**2. The throttle learned only from successes.** It read the rate-limit headers off
200s. A run that opened by being rate-limited therefore never learned anything, and
re-fired into the same wall at the floor interval for the entire batch — which is what
made an earlier 150-account attempt spend thirty minutes to reach simulated day 5 with
zero accepted proposals. 429 responses carry the same headers; it reads them now.

**3. A per-day 429 was retried like a per-minute one.** Each proposal burned both
attempts against a wall it could not pass, and the run continued to the end reporting a
tidy column of `failures`. `DAILY_LIMIT_MARKERS` now stops the arm on the first one,
records `http_429_daily_limit`, and the report says in as many words that the row is
part model and part planner and is **not** a measurement. That warning fired on this
very run: the wall arrived at call 188 of 527 consulted decisions.

**4. The agreement rate was the planner agreeing with itself.** The ledger recorded
`fell_back` but not *whether the proposer was asked at all* — and the proposer is asked
only where the planner's top two are within `proposer_margin`, which is a small
minority of decisions. So `_agreement` was computed over 6,700 decisions the model
never saw, of which it "agreed" with 100% by construction. It reported 89.9%.

`consulted` is now in the PROPOSE event and the denominator is `consulted and not
fell_back` — decisions the model actually answered. That is a two-stage correction:
6,747 → 527 (asked) → 185 (answered). The reported figure moved 89.9% → 98.3% → **95.7%**.

### What it actually says

| arm | recovery | incremental | 95% CI | agreement | n answered |
|---|---|---|---|---|---|
| rules | 37.9% | −₹1,41,318 | [−₹6,47,081, ₹2,84,503] | 0.9% | 232 |
| planner-argmax | 37.6% | −₹1,46,428 | [−₹6,68,012, ₹2,72,245] | — | — |
| groq (gpt-oss-20b) | 37.6% | −₹1,46,428 | [−₹6,68,012, ₹2,72,245] | 95.7% | 185 |

- **The LLM confirms the planner.** 177 of 185 answered decisions were the planner's own
  argmax. The 8 disagreements were 7× `WAIT → SEND_PAYMENT_LINK` and 1× `WAIT →
  SEND_MESSAGE` — uniformly in the direction of acting rather than waiting. None of them
  changed whether an account settled: the two arms recover the identical rupee total and
  differ only in one contact and 15 paise of cost.
- **The heuristic disagrees with the MDP almost totally** — 0.9% agreement over 232
  decisions, mostly `WAIT → RETRY_DEBIT` (126), `→ SEND_PAYMENT_LINK` (46), `→
  SPLIT_DEBIT` (38) — and lands ₹5,109 *ahead*, well inside the interval. On this batch
  the MDP does not beat a calendar. That contradicts the ₹6.71L the M9 run reported for
  the MDP over the heuristic, and the difference is n and seed, not a fix: this is 600
  accounts against 3,000.

**The honest reading:** at 600 accounts nothing here is measurable, and the ablation says
so three times over. What it does establish is that the LLM is an expensive way to reach
the planner's answer — 185 calls, ~200,000 tokens, a whole day's free-tier budget, to
change 8 decisions and ₹0. Ship the planner. The measurement infrastructure that made
that legible is the deliverable; the arm that lost is not a failure of the project.

**Still unmeasured:** the groq arm past decision 185. The budget resets daily and one
clean 600-account arm needs roughly three days of it, or a paid tier.

---

## 2026-09-05 — six things that existed on paper and could not happen in a run

A sweep for bugs, prompted by nothing breaking. That is the point: every defect below
sat behind a *passing* test. `test_gate_denies.py` proves each rule denies when handed a
context that violates it, and none of that helps if the runner can never build that
context or the executor quietly drops the action the rule governs. The seam between the
unit test and the run is where all six were.

**1. Two of the three notice rules were unreachable.** POL-NOTICE-001 and POL-NOTICE-003
had the *identical* predicate — `_live_notice(...) is not None`, which requires an
unspent notice. Rules short-circuit on the first DENY, so -001 always fired first and
-003 could never be the rule that failed. Across every batch on disk it has zero
denials. A customer whose notice had already been spent by an earlier attempt was told
"no pre-debit notice was issued", which is a different compliance failure with a
different remedy. `_matching_notice(unspent=...)` now splits existence-and-timing (-001,
-002) from consumption (-003), and the two tests that hedged with
`assert rule_id_failed in (...)` now name one rule each.

**2. An opt-out never reached the record the gate reads.** `World.contact` set
`state.opted_out` and a terminal state; POL-STOP-001 reads `consent.opted_out_at`, which
nothing ever wrote. The rule was structurally unable to fire however many customers
withdrew. In practice the terminal state stopped the account one rule later on
POL-STOP-005 — the right outcome for the wrong reason, and only because opt-out happens
to be terminal here. The withdrawal is now written onto the consent record.

**3. `VOICE_CONFIRM_PTP` had no execution path at all.** `_act` dispatches on
ActionType and simply fell off the end for it. The planner priced the most intrusive
action in the set — cost 360 paise, harm weight 2.0, the highest of both — could select
it, and *nothing happened*: no gate, no contact, no cost, no harm counter. A free WAIT
wearing a different label, which also made POL-AI-001, POL-AI-002 and POL-FREQ-003
unenforceable in a run. It now routes through the PTP capture over `Channel.VOICE` with
`disclosure=True` set in deterministic code, and `_act` ends in a recorded fallback so
falling off the end is no longer silent.

**4. The voice budget was a constant.** `_Executor.context` reported
`voice_remaining_cycle=cfg.budgets.voice_per_cycle` on every evaluation, so POL-FREQ-003
("at most one automated voice call per cycle") could never bind. A cap needs a counter;
`CycleState.voice_calls` is it.

**5. `SPLIT_DEBIT` presented the full amount under a split's name.** It shared a branch
with `RETRY_DEBIT`, so choosing it re-presented the *whole* amount — the exact amount
that had just been refused — and wrote it to the ledger as a split. It is the designed
remedy for LIMIT_EXCEEDED, and a test pins that intent, so it stays eligible; the
planner cannot select it anyway, because the MDP gives it RETRY_DEBIT's exact success
model at twice the cost and three times the harm, which is strict domination. It is now
refused loudly into the trail. **Left unbuilt deliberately:** a real split needs a
notice per presentation (POL-NOTICE-003 is one notice per attempt), partial settlement,
and a per-part cap check. That is a feature, and half-building it would move the
headline for reasons nobody could audit.

**6. The UI put provider and model text into `innerHTML` unescaped.** Razorpay's own
error strings reach the page as `RZP:<reason>` through the code map, and a PROPOSE note
carries the model's rationale — both arrive via the ledger, neither is ours. One `esc()`
helper now wraps every server-derived interpolation, `href` is rendered as a link only
when the URL is `https:`, and path segments are encoded. Also: `_batch_path` accepted any
path off the disk, so `GET /batches/../../../<anything>/verify` opened it as SQLite. The
app has no auth by design; the one thing it must therefore not do is read outside
`data/`.

Nothing here moved the headline. Every one of them was a way for a future change to be
wrong without anything failing — which is the failure mode this project is supposed to be
about. `tests/test_reachability.py` covers the class: an action the executor cannot
perform, a rule whose context cannot be built, a path that escapes `data/`.

Also fixed while in here: `rr ablate` re-run into an existing directory died on
`UNIQUE constraint failed: events.event_id`, because it simulated over the previous
run's file. It reads as a ledger bug rather than as "you ran this twice". Each arm's
database is now removed before it is regenerated.

---

## 2026-09-05 — building SPLIT_DEBIT, and what it turned out to be worth

Previously refused loudly into the trail as unimplemented. Now built, and the honest
answer to "what does a split buy you" turned out to be narrower and more interesting
than the obvious one.

### It buys the ceiling, not the balance

The parts sum to the cycle amount — POL-AMT-001 reconciles them, so a split collects what
is owed, no more and no less. That means it buys **nothing** against a shortfall:
needing the whole amount is needing the whole amount, and `p_funds` is unchanged. What it
buys is the *per-transaction* ceiling. Two presentations of ₹10,000 clear an RBI
AFA-free cap of ₹15,000 that one of ₹20,000 does not.

That single sentence decided the whole design. It is why the MDP needed no new funds
model, why POL-AFA-001 had to move from `amount_paise` to per-presentation, and why the
action belongs to LIMIT_EXCEEDED rather than INSUFFICIENT_FUNDS.

### What a presentation costs

Everything scales with n, and the value function now says so: n fees, n units of harm, n
attempts out of the budget, and `infra ** n` — every part has to clear for the cycle to
settle. `SPLIT_DEBIT` in config moved from a flat 400 paise / 0.3 harm to 200 / 0.1, the
same as the retry it is n of, because the executor charges the rail fee per presentation
and the planner has to optimise against the number the batch actually spends.

`split_max_parts: 3`. Beyond that the arithmetic stops being a remedy and starts being
harassment; an amount needing four presentations wants a payment link.

### One notice per presentation

POL-NOTICE-003 is one notice per attempt, so a two-part split needs two *distinct*
receipts — matching one receipt twice is precisely the double-spend the rule forbids.
`notify` therefore returns a tuple, the gate's `_match_notices` matches greedily without
reuse, and ledger invariant 8 now checks one referenced notice per part rather than
looking at `notice_ref` alone and being satisfied by a single receipt for three debits.

The notice has to be issued for the shape the debit will take, which is knowable a day
ahead because the ceiling is fixed. **But only the agent asks for split notices.** The
merchant baselines present the full amount whatever the ceiling says — that is what
makes them baselines — and deriving the split inside `notice()` silently improved the
control group, which understates the agent by exactly the improvement. It showed up as
`test_fixed_beats_nothing` failing: 511 recoveries against 517.

### Four ways the action was unreachable

Building it was quick. Making it *happen* took four fixes, each one two parts of the
system holding different beliefs:

1. **The cause matrix never offered it.** A split was only in `ALLOWED` for
   LIMIT_EXCEEDED. But the ceiling is structural — it binds whatever the money failed
   for. 74 of 600 accounts in one batch were over the cap with a feasible split, most of
   them coded INSUFFICIENT_FUNDS or TRANSIENT_INFRA; the retry was stripped as
   impossible, the split was never offered, and the agent issued a notice it could never
   act on and then waited out the horizon. The split now *replaces* the retry wherever
   the amount is over the ceiling.
2. **The MDP never priced it.** With the runtime set fixed, `eligible_actions` offered
   the split and `solve()` still had not priced it for those causes, so `q_at` returned
   `-inf` and it was silently unreachable. Wherever a retry is worth pricing, so is the
   split.
3. **`notice_pending` asked about the wrong amount.** It looked for a receipt matching
   the *cycle total*, while a split's notices are per part — so it was false for ever,
   the agent re-noticed every single day (663 notices for 376 retries), and the debit
   those notices were for could never be reached. Notices dropped to 450 once fixed.
4. **Two definitions of "the mandate we would debit".** `eligible_actions` took the
   lowest-sorted rail; the executor takes the first active mandate. When they differed,
   the ceiling was computed against one and the debit presented on another: 17
   POL-NOTICE-001 denials on one batch, all of them the two definitions disagreeing.
   There is one definition now.

And one found on the way, of the same class as the opt-out that never reached the consent
record: **the gate was reading stale notice receipts.** `CycleState.notices` holds the
receipt as issued and never learns it was spent, so every consumed notice looked live and
POL-NOTICE-003 could not fire in a run. The adapter refusing the presentation kept
`notice_window_violations` at zero, but a backstop is not enforcement. The gate context
now reads the adapter's own copy.

### Result

On 600 accounts, seed 42: 33 splits, of which most collect in full and some collect part
one and are refused on part two. A partial collection moves `paid_paise` and leaves the
cycle unsettled — `settled` keeps meaning the whole amount arrived, which every rate on
the scoreboard depends on. Notice-window violations stay at 0 and all three policies
verify.

**Not claimed:** that this moves the headline. It reaches 33 accounts in 600 and the
batch-level effect is well inside the interval. What it fixes is a hole in the action
set — an over-cap account previously had no debit available at all and sat out its
horizon behind a notice the agent kept re-sending.

---

## 2026-09-05 — the demo page, and a headline that moved while nobody was looking

### The batch had to be regenerated, and the number changed a lot

`config/default.yaml` changed when the split debit was built (`split_max_parts`, and
`SPLIT_DEBIT` repriced per presentation), so `world_hash` moved and `data/demo.db` was a
record of a world that no longer exists. Re-simulated and re-run on the same seed:

| | before the split work | after |
|---|---|---|
| agent recovery | 39.2% | **52.3%** |
| holdout, rate-adjusted | 38.0% | **38.0%** — unchanged |
| incremental | ₹1,48,412 (1.2%) | **₹17,39,386 (14.4%)** |
| 95% CI | [−₹8.4L, +₹10.5L] — contains zero | **[₹7.3L, ₹26.7L] — excludes zero** |

**The holdout is identical to the paisa**, which is the check that matters: the control
arm is untouched by anything the agent does, so the whole movement is in the treatment
arm and none of it is the batch being different. `rr verify` clean, notice-window
violations 0.

Where it came from: 145 split presentations across 123 accounts, of which 73 settled.
Over-cap accounts are *by construction the large ones* — that is what being over a
per-transaction ceiling means — and before the split worked they had no debit action
available at all. The agent was sitting out the heavy tail of its own batch. The earlier
₹1,48,412 was a measurement of a broken agent, taken honestly; this is the same apparatus
on a fixed one.

**Do not quote the old figure**, and do not quote this one as though it were the earlier
result improved by tuning. Nothing about the measurement changed.

### The page

Rebuilt from a dark GitHub-flavoured dashboard into a **settlement report**. The reason is
not taste: the argument this project makes is "do not trust the number, check the trail",
and a dashboard is a form that asks to be believed. Three choices carry that.

**The interval is the hero.** It used to be a parenthesis in 11px type under a big number.
It is now a drawn axis with zero as a full-height rule, the estimate as a tick, and the
band tinted on both sides of zero — so an interval that contains zero *looks* like it
contains zero instead of being explained away in a caption. The page had to work in both
states and it does; the copy branches on which one is true. Every diverging bar further
down reuses the same zero-axis grammar, so the page has one idea rather than five charts.

**Every section names its source.** `GET /batches/{b}/scoreboard · ledger stage OBSERVE`
sits beside each heading. That is not an eyebrow decoration — numbered `01 / 02 / 03`
markers would have been — it is the one affordance that lets a sceptical reader go and
check, which is the whole thesis made structural.

**Monospace is the display voice**, at 54px with tight tracking, not just for code. The
content is machine records: NPCI return codes, rule IDs, sequence numbers, hash-chained
events. Setting the headline in a sans would be dressing them up as something friendlier
than they are. No webfont — a demo that loses its typeface on conference wifi is worse
than one that never had it.

Colour is reserved for the experiment: teal for the agent acting, an inert grey for the
holdout, and green/red only where a sign is being claimed. If a pixel is coloured it
means something.

### Two bugs the redesign surfaced

- **The Razorpay panel was unreachable.** It rendered only for the selected batch, and
  the batch holding the real provider ids is deliberately never given a policy — so it
  never appeared in the picker and the most checkable artefact on the page could not be
  shown. The live slice is its own artefact now: the page looks in the selected batch
  first, then anywhere.
- **An un-run batch returned a 500.** Every scoreboard figure divides by an arm that does
  not exist yet. It now returns 409 and says which command to run.

Contrast was measured rather than eyeballed: the tertiary tokens were at 3.8–4.4:1 on
their own grounds in both themes and are now ≥4.5. Mobile at 375px has no horizontal
overflow. Dark mode is a designed palette, not an inversion.
