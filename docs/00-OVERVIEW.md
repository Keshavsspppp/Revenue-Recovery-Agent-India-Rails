# 00 — Overview

## The problem statement

Build an agent that detects revenue at risk, determines the right intervention, and
executes a bounded recovery workflow. The bar: **measured money recovered across a
batch, with compliant escalation, stopping rules, and an audit trail.**

## Where most attempts fail

Not at detection. Detection is a database query. They fail at *measured*.

A large share of failed recurring payments recover with no intervention at all — the
salary lands, the customer sees their bank's SMS, and the next scheduled attempt
succeeds. Any system that counts every recovered rupee it touched is reporting the
passive base rate with its own logo on it. Layered on top, last-touch attribution has
been reported to overstate the impact of dunning messages by roughly 30–60%.

So the first design decision, before any agent code exists, is the **holdout arm**.

## The India-specific thesis

Outside India, failed-payment recovery is a retry-timing problem, and it is solved.
Stripe's Smart Retries is an AutoML ensemble over 500+ features that evolved from an
XGBoost model. You are not beating that in a weekend, and you should not try.

On Indian rails the binding constraint is different. RBI's consolidated **Digital
Payments E-Mandate Framework** (in force 21 April 2026, unifying the earlier circulars
across cards, UPI and PPIs) requires a **pre-debit notification to the customer at
least 24 hours before each debit**, carrying merchant name, amount, date, mandate
reference and an opt-out. Commentary on the framework indicates this applies to
re-presentations too — each retry needs its own fresh 24-hour notice, and out-of-window
attempts are rejected at the rail before they reach the issuer.

> ⚠️ **Verify this before you build the pitch on it.** The per-retry notification
> requirement comes from secondary commentary, not the circular text. Read the RBI
> framework directly. If retries turn out to be exempt from fresh notice, the budget
> framing still holds — an attempt still costs a customer contact and mandate
> goodwill — but the scarcity is softer and you must say so. See `12-GLOSSARY.md`.

The consequence is structural:

| | Card-first market | Indian rails |
|---|---|---|
| Cost of an attempt | ~free, instant | 24h lead time + a customer contact |
| Attempts per cycle | many | a handful |
| Optimisation | *when* to retry | *whether to spend* an attempt at all |
| Failure repair | account updater | **re-registering the mandate on a different rail** |

That last row is the part nobody has productised, because outside India there is
nothing to arbitrage. India gives you multiple rails against the same customer with
different AFA thresholds and different failure modes. A mandate that is structurally
dead on eNACH (`AP17`, NRE account) is not a lost customer — it is a customer who
needs a UPI Autopay mandate. A ₹18,000 debit that keeps tripping AFA is a customer who
needs a split or a payment link.

## What this project is

**Failed recurring-payment recovery on Indian rails, framed as attempt-budget
allocation under compliance constraints, with a measurement harness that proves the
lift is real.**

The agent's state includes days to cycle end, attempts remaining, notices spent,
contact-fatigue budget, a posterior over failure causes, and an estimate of when the
customer's money next arrives. Its action space is a small closed set. Its objective
is net recovery minus cost minus a priced harm term. Its stopping rule is economic,
not a constant.

## Non-goals

Say these out loud; scope discipline is a feature.

- **Not** a retry-timing model. See above.
- **Not** checkout-abandonment recovery. You cannot prove the money was yours: a large
  share of abandoners return unprompted, and unsolicited outreach to non-customers has
  a much heavier consent surface.
- **Not** a general collections platform. One vertical slice, measured properly.
- **Not** a voice product. Voice is one narrow scripted flow, or it is cut. See
  `05-POLICY-ENGINE.md` §Voice and `10-BUILD-PLAN.md` M9.
- **Not** production-integrated. Rails are simulated behind an adapter interface.

## Optional second intervention: the 43B(h) lever

If there is time after M8, add one B2B receivables action type. Section 43B(h) of the
Income Tax Act requires buyers to pay registered micro and small enterprises within
45 days where a written agreement exists, or 15 days where none does, counted from
delivery rather than invoice date. Miss it and the buyer loses the deduction for that
principal in that financial year (it shifts to the year of actual payment) and owes
compound interest at three times the RBI bank rate, itself non-deductible.

So the correct chaser to an MSME's overdue buyer is not "please pay." It is a computed,
dated statement of the buyer's own tax exposure. That is arithmetic rather than
persuasion, it is fully auditable, and no dunning tool ships it. The agent's job is
eligibility (is the vendor micro or small — traders are excluded; is there a written
agreement) and escalation on a legal clock.

This is a bolt-on action type, not a second project. Do not start it before M8 is green.

## The three things that decide whether this clears the bar

1. **A holdout arm**, assigned at the failure event, so the headline is incremental.
2. **A policy engine that can say no**, in code, before any action reaches a customer —
   and a log that proves it said no.
3. **A stopping rule with an economic basis**: stop when the expected value of the best
   remaining action is below its cost, not after a hardcoded four tries.

Everything else is implementation.

## Regulatory surface (summary)

Detail and citations in `12-GLOSSARY.md`. Encoded rules in `05-POLICY-ENGINE.md`.

| Regime | Constrains | Key rule |
|---|---|---|
| RBI e-mandate framework (21 Apr 2026) | when/how you may debit | ≥24h pre-debit notice; AFA on registration, modification, withdrawal, first transaction; no AFA on subsequent debits ≤₹15,000 (≤₹1,00,000 for insurance, mutual funds, credit-card bills) |
| RBI recovery conduct (draft, proposed 1 Jul 2026 ⚠️) | when/how you may contact | 08:00–19:00 only; calls recorded with prior intimation; no contact during bereavement/calamity/festivals; no recovery while subjudice; lender liable for agent conduct |
| TRAI TCCCPR / DLT | which number and template | 1600 series for service/transactional from RBI/SEBI/IRDAI/PFRDA-regulated entities to existing customers; 140 for promotional; registration with telecom providers; DND scrubbing on promotional |
| DPDP Act 2023 + Rules 2025 | what data you hold and why | purpose limitation, minimisation, erasure, notice & consent |
| RBI FREE-AI (advisory, 13 Aug 2025) | how the AI behaves | "disclosure and the right to override"; "accountability regardless of autonomy"; grievance redressal for AI-driven decisions |

The FREE-AI sutras are why the ledger has a `human_override` field and why the voice
flow opens with an AI disclosure line. Those are not decoration; they are the
framework's two most quotable requirements made concrete.
