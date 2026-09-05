# 07 — Proposer (Groq)

Module: `app/propose/`

The LLM's job is narrow and its blast radius is bounded. It **proposes** one action from
a pre-computed eligible set, with a rationale and cited evidence. A deterministic
executor **disposes**: it fills amounts, schedules, templates and recipients, and refuses
anything the gate has not allowed.

> **The LLM never emits an amount, a phone number, a schedule, or customer-facing text.**
> There is no field for any of those in its output schema. That is not a guardrail bolted
> on afterwards — it is the shape of the interface.

---

## Why an LLM is here at all

Be honest about this, including on stage. The LLM does **not** improve retry timing — the
planner does that with arithmetic, and better. What the LLM plausibly adds:

- reading unstructured signals (customer replies to `REQUEST_PTP`, dispute text, distress
  language) into structured evidence,
- choosing among several *near-equal-EV* actions using context the state vector does not
  encode,
- producing the human-readable rationale that goes in the audit trail.

You will measure whether it actually adds any of that (§Ablation). If it does not, **that
is a finding worth reporting**, not a failure to hide. "We ran the ablation and the LLM
added 0.4pp, inside the noise, so we ship the deterministic planner" is a stronger
answer than an unfalsifiable claim.

---

## Groq setup

Groq is OpenAI-compatible.

```
Base URL:  https://api.groq.com/openai/v1
Endpoint:  POST /chat/completions
Auth:      Authorization: Bearer $GROQ_API_KEY
```

`.env.example`:

```
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b
GROQ_MODEL_FALLBACK=llama-3.3-70b-versatile
GROQ_TIMEOUT_S=6
GROQ_MAX_RETRIES=1
```

### Model choice

Groq supports **structured outputs** via `response_format: {"type": "json_schema"}`.
Strict mode (`strict: true`) — token-level constrained decoding, guaranteed schema
conformance — is supported on a subset of models; at the time of writing that includes
the `openai/gpt-oss-*` family. Best-effort mode works on more models but *may* return
valid JSON that does not match your schema.

Use **strict mode** and treat schema conformance as guaranteed; use best-effort only as
the fallback path, where you validate defensively.

> Verify the current strict-mode model list and IDs before you build:
> `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`
> and the structured-outputs page in Groq's docs. Model IDs churn; pin what you verified
> and record it in `docs/DECISIONS.md`.

Strict mode requires every property to be in `required` and `additionalProperties: false`.
Note that **streaming and tool use are not supported with structured outputs** — you need
neither here.

---

## Output schema

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "recovery_action_proposal",
    "strict": true,
    "schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["action_type", "target_rail", "channel", "rationale",
                   "evidence_refs", "confidence"],
      "properties": {
        "action_type": {
          "type": "string",
          "enum": ["SEND_PREDEBIT_NOTICE","RETRY_DEBIT","SEND_MESSAGE",
                   "SEND_PAYMENT_LINK","REREGISTER_MANDATE","SPLIT_DEBIT",
                   "REQUEST_PTP","VOICE_CONFIRM_PTP","OFFER_ACCOMMODATION",
                   "ESCALATE_HUMAN","WAIT","CLOSE"]
        },
        "target_rail": {
          "type": ["string","null"],
          "enum": ["UPI_AUTOPAY","ENACH","CARD_EMANDATE","PAYMENT_LINK", null]
        },
        "channel": {
          "type": ["string","null"],
          "enum": ["SMS","WHATSAPP","EMAIL","VOICE","PUSH", null]
        },
        "rationale":     { "type": "string", "maxLength": 280 },
        "evidence_refs": { "type": "array", "items": {"type":"string"}, "maxItems": 6 },
        "confidence":    { "type": "number", "minimum": 0, "maximum": 1 }
      }
    }
  }
}
```

No `amount_paise`. No `scheduled_for`. No `template_id`. No `phone`. No `message_body`.
`tests/test_no_llm_amounts.py` asserts the schema contains none of those keys — so a
future edit that adds one fails CI.

---

## Prompt

**System:**

```
You select one recovery action for a failed recurring payment on Indian payment rails.

You are given: a cause posterior, an account's observable history, remaining budgets,
and a list of ELIGIBLE actions. You may only choose an action from ELIGIBLE.

Rules you must respect:
- INSUFFICIENT_FUNDS is a timing problem. Messages cannot create money. Prefer WAIT or
  a notice timed after the customer's estimated inflow date.
- TRANSIENT_INFRA means nothing is wrong on the customer's side. Do not message them.
- MANDATE_INVALID means the mandate is structurally broken. Retrying the same rail
  cannot succeed; repair onto another rail instead.
- Every RETRY_DEBIT consumes a pre-debit notice issued at least 24 hours earlier.
- Contacts are scarce and annoy the customer. WAIT is a legitimate, often optimal choice.
- If a promise-to-pay is open, choose WAIT.

Cite evidence by the exact keys given to you. Do not invent facts, amounts or dates.
Return only the JSON object.
```

**User:** a compact JSON rendering of `AgentState` plus the eligible set. Keep it small
and stable — no free prose, no PII:

```json
{
  "cause_posterior": {"INSUFFICIENT_FUNDS":0.71,"TRANSIENT_INFRA":0.19,"UNKNOWN":0.10},
  "days_left": 21,
  "attempts_left": 3,
  "contacts_left": 2,
  "notice_pending": false,
  "estimated_inflow_in_days": 3,
  "inflow_confidence": 0.62,
  "attempts_made": 1,
  "last_rail_code": "AP01",
  "mandates": [{"rail":"ENACH","status":"ACTIVE","cap_paise":2000000}],
  "alt_rail_available": true,
  "ptp_open": false,
  "hardship_score": 0.12,
  "merchant_category": "SUBSCRIPTION",
  "amount_band": "1000-2500",
  "eligible": ["WAIT","SEND_PREDEBIT_NOTICE","REQUEST_PTP","SEND_PAYMENT_LINK"]
}
```

`amount_band` rather than the amount: the model does not need the rupee figure to pick an
action, and withholding it makes it structurally impossible for the model to emit one.

**No PII.** No name, phone, email, account number, or city. The model sees a state
vector, not a person. This is DPDP data-minimisation made concrete, and it is a good
thing to say out loud.

---

## Client behaviour

```python
class GroqProposer:
    def propose(self, state, posterior, eligible) -> Proposal | None:
        # temperature 0.0, max_tokens 300, timeout GROQ_TIMEOUT_S
        # on timeout / 429 / 5xx: ONE retry, then return None
        # on invalid JSON or action not in `eligible`: log PROPOSER_INVALID, return None
```

Rules:

- **Never loop the LLM.** One retry, then fall through. An agent that re-prompts until it
  gets an answer it likes is an agent with no bound on cost or latency.
- Returning `None` is normal and safe: the runner uses `planner.best(state, eligible)`.
- Log every call to the ledger's `model` field:
  `{"proposer": "groq:openai/gpt-oss-120b", "latency_ms": 412, "fell_back": false}`.
- Cache by a hash of the user payload. Identical states recur constantly across 2,000
  accounts, and caching cuts both cost and demo latency. Record cache hits.

## Deterministic fallback

`RulesProposer` implements the same interface using the cause → eligible matrix plus the
planner's `Q` values. **The repo must run end-to-end with no `GROQ_API_KEY`**, labelled
`proposer=rules` in the ledger.

This is not a degraded mode. It is the control arm.

---

## Ablation — the experiment that makes the LLM claim honest

Run the identical batch, identical seed, identical policy version, three ways:

| Run | Proposer | What it isolates |
|---|---|---|
| A | `rules` | Deterministic floor |
| B | `planner-argmax` | Pure MDP, no proposer layer |
| C | `groq` | LLM proposal, planner fallback |

Report `net_incremental` for all three with confidence intervals, plus:

- **agreement rate** — how often the LLM picked the planner's argmax,
- **override value** — when they disagreed, mean realised outcome of the LLM's choice
  minus the planner's, computed by seeded replay of the same world under both actions
  (see `04-CAUSE-TAXONOMY.md` §counterfactual regret),
- **invalid rate** — proposals outside the eligible set,
- **cost** — tokens, rupees, p50/p95 latency.

If C ≈ B within the interval, say so plainly and ship B. The measurement infrastructure
that let you find that out is the actual achievement, and it is the thing that
distinguishes this project from a demo with an LLM bolted to a database.
