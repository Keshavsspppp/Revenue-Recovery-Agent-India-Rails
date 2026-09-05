# 12 — Glossary, sources & verification checklist

## Verify before you present

Everything below marked ⚠️ rests on secondary commentary or draft rules. Read the primary
text yourself. This section exists so that you are the one who finds the gap, rather than
a judge.

| # | Claim | Status | How to verify |
|---|---|---|---|
| V1 | **Each retry requires its own fresh 24-hour pre-debit notification**, and out-of-window attempts are rejected at the rail | ⚠️ Secondary commentary. **Load-bearing** — the entire attempt-budget thesis rests on it | Read the RBI Digital Payments E-Mandate Framework text directly on rbi.org.in. If retries are exempt, change `POL-NOTICE-003` and **re-run the batch** |
| V2 | RBI recovery-conduct norms (08:00–19:00, recorded calls, festival/bereavement suppression) are **in force** | ⚠️ Draft for consultation, proposed effective 1 July 2026. Today is later than that — confirm whether they were finalised, and on what terms | rbi.org.in notifications; search for the final master direction |
| V3 | AFA-free caps of ₹15,000 / ₹1,00,000 and the category list | Reported consistently across sources; still confirm the current category list | RBI e-mandate framework text |
| V4 | AP-series code numbers and meanings | ⚠️ From published summaries of NPCI/2024-25/NACH/006. Lists drift — that is the point of §04 | Your PSP's own current code list (Razorpay/Cashfree developer docs) |
| V5 | Vendor success-rate figures (92–96% blended UPI, 85–90% cards, metro/tier-3 gap, evening-peak drop) | Vendor-published, self-reported, directional | Label them as such on any slide. Do not present as measurements |
| V6 | Last-touch attribution overstates dunning-message lift by 30–60% | Single secondary source | Cite as reported, not as established. Your own holdout is the primary evidence you actually rely on |
| V7 | Groq strict-structured-output model IDs | Changes frequently | `GET https://api.groq.com/openai/v1/models` and Groq's structured-outputs docs. Pin what you verified in `docs/DECISIONS.md` |
| V8 | DPDP consent-manager deadline (13 Nov 2026) | ⚠️ Reported; regulator status was itself unsettled | meity.gov.in and the DPDP Rules text |

**Regulatory positions in this repo are stated as of September 2026.**

---

## Glossary

| Term | Meaning |
|---|---|
| **AFA** | Additional Factor of Authentication. Required at mandate registration, modification, withdrawal and first transaction; not on subsequent debits within the applicable cap |
| **Arm** | `treatment` or `holdout`. Assigned at the failure event, immutable thereafter |
| **Attempt budget** | The small number of debit attempts available per cycle, scarce because each is gated by a 24-hour notice and costs a customer contact |
| **Cause class** | The canonical failure taxonomy (`01-DOMAIN-MODEL.md`). Latent; observed only through noisy rail codes |
| **Circular mean** | The averaging method used for inflow-day estimation, because day 30 and day 1 are two days apart, not twenty-nine |
| **DLT** | Distributed Ledger Technology platform for TRAI commercial-communication registration. Senders, headers and templates are pre-registered |
| **DND** | Do Not Disturb registry. Blocks promotional traffic; service/transactional contact to existing customers is treated differently |
| **eNACH** | Electronic National Automated Clearing House. Bank-account-based recurring debit mandates |
| **e-mandate** | RBI's framework for recurring payments across cards, UPI and PPIs |
| **FREE-AI** | RBI's Framework for Responsible and Ethical Enablement of AI. Advisory. Source of "disclosure and the right to override" and "accountability regardless of autonomy" |
| **Holdout** | The randomised control arm receiving the merchant default policy. Its recovery is the self-cure baseline |
| **Incremental recovery** | `(treatment rate − holdout rate) × at-risk value`. The only honest headline |
| **Inflow phase** | The estimated day of month the customer's money arrives, inferred from past successful debits only |
| **λ (lambda)** | The harm price in the planner's objective. The single dial producing the recovery/harm frontier |
| **Notice coupling** | The constraint that `RETRY_DEBIT` requires a `SEND_PREDEBIT_NOTICE` issued ≥24h earlier — so an attempt is committed a day before you know whether the money arrived |
| **NPCI** | National Payments Corporation of India. Operates UPI and NACH; publishes the return-code taxonomy |
| **PTP** | Promise to Pay. A commitment with a verifiable outcome and a state machine, not a CRM note |
| **Self-cure** | Recovery that happens with no intervention — the salary lands, the default retry succeeds |
| **Subjudice** | Matter before a court. RBI draft norms bar recovery activity while a matter is subjudice |
| **TCCCPR** | TRAI's Telecom Commercial Communications Customer Preference Regulations |
| **Terminal state** | An enumerated reason the workflow stopped (`01-DOMAIN-MODEL.md`). Every closed cycle has exactly one |
| **1600 / 140 series** | TRAI-designated number series: 1600 for service/transactional calls from RBI/SEBI/IRDAI/PFRDA-regulated entities to existing customers; 140 for promotional |
| **Udyam** | India's MSME registration. Determines whether Section 43B(h) applies to a supplier |

---

## Sources

Regulatory and rail mechanics:

- [MediaNama — RBI mandates additional factor authentication for e-mandates](https://www.medianama.com/2026/04/223-rbi-additional-factor-authentication-e-mandates/) — framework in force 21 Apr 2026; AFA scope; ₹15,000 / ₹1,00,000 caps; 24-hour pre-debit notification; FASTag/NCMC exemption
- [Slicker — Country-specific retry rules: India RBI](https://www.slickerhq.com/resources/blog/country-specific-retry-rules-rbi-direct-debit-paypal) — ⚠️ **V1**: fresh notification per retry, out-of-window rejection at the rail
- [Razorpay — e-NACH & UPI Autopay collections playbook 2026](https://razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026) — rail selection, mandate limits, notification fields
- [TaxGuru — Revised rejection reason codes for NACH e-mandates](https://taxguru.in/finance/revised-rejection-reason-codes-nach-e-mandates.html) — NPCI/2024-25/NACH/006, 27 Nov 2024; 20 added / 33 revised / 22 removed; AP-series meanings

Benchmarks (vendor-published, directional):

- [ProductGrowth — UPI payment success rate benchmarks](https://productgrowth.in/insights/fintech/upi-payment-success-rates/) — NPCI technical/business decline targets; failure-cause mix
- [Razorpay — Payment success rate optimization in India](https://razorpay.com/blog/payment-success-rate-optimization-india/) — method-wise success rates; metro/tier-3 gap; evening-peak degradation

Measurement:

- [Yuno — How to actually measure failed-payment recovery](https://www.y.uno/en/blog/how-to-actually-measure-failed-payment-recovery) — the three denominators; last-touch overstating dunning lift by 30–60%
- [Stripe — How we built it: Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries) — AutoML ensemble, 500+ features; what you are explicitly not trying to out-model

Conduct, communications and AI:

- [Upstox — RBI draft loan recovery norms](https://upstox.com/news/business-news/financial-regulations/rbi-proposes-overhaul-of-loan-recovery-norms-draft-rules-ban-harassment-cap-calls-to-8am-7pm/article-189398/) — ⚠️ **V2**: 08:00–19:00 window; recorded calls with prior intimation; prohibited practices; lender liability
- [MediaNama — TRAI clarification on 1600 and 140 number series](https://www.medianama.com/2026/07/223-trai-releases-clarification-designated-promotional-transactional-number-series/)
- [Oli AI — Is AI voice calling legal for loan collections in India](https://oliai.in/blog/ai-voice-calling-legal-loan-collections-india/) — DLT registration, consent, disclosure, recording
- [RBI FREE-AI framework summary](https://www.humaineeti.ai/resources/rbi-free-ai-framework) — report 13 Aug 2025; 7 sutras; advisory status
- [ComplianceHub — DPDP consent manager deadline](https://compliancehub.wiki/india-dpdp-consent-manager-november-2026-phase-two-deadline-compliance/) — ⚠️ **V8**

B2B receivables (optional lane):

- [Cashfree — MSME 45-day payment rule and Section 43B(h)](https://www.cashfree.com/blog/msme-45-days-payment-rule-section-43bh-explained/) — 15/45-day deadlines from delivery; deduction deferral; compound interest at 3× RBI bank rate, non-deductible; traders excluded

Implementation:

- [Groq — Structured outputs](https://console.groq.com/docs/structured-outputs) — strict vs best-effort mode, model support, schema requirements
- [Groq — API reference](https://console.groq.com/docs/api-reference) — base URL, chat completions, model list

---

## `docs/DECISIONS.md`

Keep a running log. One entry per ambiguity you resolved:

```markdown
## 2026-09-04 — Per-retry notice reading
Encoded POL-NOTICE-003 as requiring a fresh notice per attempt (the strict reading).
Basis: secondary commentary (V1); primary text not yet checked.
Consequence: attempts_left defaults to 4 rather than unbounded.
If falsified: relax POL-NOTICE-003, raise the attempt budget, re-run batch, update the
headline. The budget framing survives either way because an attempt still costs a contact.
```

That file is also your evidence of engineering judgement when someone asks why you chose
something. It is worth more than a tidier README.
