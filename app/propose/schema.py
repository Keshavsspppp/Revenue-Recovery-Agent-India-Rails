"""The proposer's output schema, and the reason it looks like this.

The LLM selects one action from a pre-computed eligible set and says why. A deterministic
executor disposes: it fills the amount, the schedule, the template and the recipient, and
refuses anything the gate has not allowed.

    The LLM never emits an amount, a phone number, a schedule, or customer-facing text.

There is no field for any of those below. That is not a guardrail bolted on afterwards —
it is the shape of the interface, and `tests/test_no_llm_amounts.py` fails if a future
edit adds one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import ActionType, Channel, Rail

#: Keys that must never appear in the schema. Adding one would move a money decision, a
#: recipient, or customer-facing wording inside the model's blast radius.
FORBIDDEN_KEYS = frozenset({
    "amount", "amount_paise", "amount_rupees", "value", "sum",
    "scheduled_for", "schedule", "date", "when", "day", "time",
    "template", "template_id", "body", "message", "message_body", "text", "content",
    "phone", "phone_number", "mobile", "email", "recipient", "to", "address",
    "name", "customer_name", "account_number", "city",
})

SCHEMA_NAME = "recovery_action_proposal"


def response_format() -> dict[str, Any]:
    """Groq structured outputs, strict mode.

    Strict requires every property in `required` and `additionalProperties: false`, which
    is exactly what is wanted here: the model cannot invent a field, so it cannot invent
    an amount even by accident.

    It also accepts only a *subset* of JSON Schema. `maxLength`, `maxItems`, `minimum` and
    `maximum` are not in it, and including them fails the whole request with an
    unhelpful HTTP 400 (`json_validate_failed`, empty `failed_generation`) rather than
    being ignored — verified against the live API, not inferred. The bounds those keywords
    expressed are enforced in `parse()` instead, which is where they belonged anyway:
    a constraint the caller checks holds whether or not the provider honours it.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": SCHEMA_NAME,
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action_type", "target_rail", "channel", "rationale",
                             "evidence_refs", "confidence"],
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": [a.value for a in ActionType],
                    },
                    "target_rail": {
                        "type": ["string", "null"],
                        "enum": [r.value for r in Rail] + [None],
                    },
                    "channel": {
                        "type": ["string", "null"],
                        "enum": [c.value for c in Channel] + [None],
                    },
                    # Bounds live in `parse()`: strict mode rejects maxLength/maxItems.
                    "rationale": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
            },
        },
    }


SYSTEM_PROMPT = """\
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
Return only the JSON object."""


@dataclass(frozen=True)
class Proposal:
    """What comes back. Note what is absent as much as what is present."""

    action_type: ActionType
    target_rail: Rail | None = None
    channel: Channel | None = None
    rationale: str = ""
    evidence_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    source: str = "rules"
    latency_ms: int = 0
    cached: bool = False

    def evidence(self) -> tuple[str, ...]:
        return (f"proposer:{self.source}",
                f"proposed:{self.action_type.value}",
                f"confidence:{self.confidence:.2f}",
                *(f"cited:{ref}" for ref in self.evidence_refs[:6]))


#: The bounds the schema cannot express. Enforced on the way in, so they hold regardless
#: of what the provider validates.
MAX_RATIONALE_CHARS = 280
MAX_EVIDENCE_REFS = 6


def parse(payload: dict[str, Any], source: str, latency_ms: int = 0,
          cached: bool = False) -> Proposal:
    """Best-effort mode can return valid JSON that does not match the schema, and strict
    mode cannot express length or range limits — so every field is bounded here rather
    than trusted."""
    return Proposal(
        action_type=ActionType(payload["action_type"]),
        target_rail=Rail(payload["target_rail"]) if payload.get("target_rail") else None,
        channel=Channel(payload["channel"]) if payload.get("channel") else None,
        rationale=str(payload.get("rationale", ""))[:MAX_RATIONALE_CHARS],
        evidence_refs=tuple(str(r) for r in
                            (payload.get("evidence_refs") or ())[:MAX_EVIDENCE_REFS]),
        confidence=max(0.0, min(1.0, float(payload.get("confidence", 0.0)))),
        source=source, latency_ms=latency_ms, cached=cached)
