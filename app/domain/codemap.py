"""The rail-code taxonomy, loaded from config/codemap.yaml.

This lives in `app/domain/` rather than `app/diagnose/` because both sides need it and
they must not import each other: the simulator needs cause -> code (what a rail emits),
the diagnoser needs code -> cause (Layer 1). One table, read in two directions, so the
two can never drift apart. See docs/04-CAUSE-TAXONOMY.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.domain.enums import CauseClass, Rail

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "codemap.yaml"

SUCCESS = "SUCCESS"
#: Rejected at the rail because we scheduled a debit outside its notice window. This is
#: our own defect, never a customer failure — it is counted separately on the scoreboard.
NOTICE_WINDOW_VIOLATION = "NOTICE_WINDOW_VIOLATION"


@dataclass(frozen=True)
class CodeMapping:
    code: str
    rail: Rail
    cause: CauseClass
    retryable: bool           # is a same-rail retry ever sensible
    customer_action: bool     # does fixing it require the customer to do something
    terminal_for_rail: bool
    description: str


@dataclass(frozen=True)
class CodeMap:
    version: str
    mappings: dict[str, CodeMapping]
    #: (rail, cause) -> the code that rail emits for that cause
    emits: dict[tuple[Rail, CauseClass], str]
    #: provider -> its own reason string -> our cause. Live rails report their own codes,
    #: not the raw NPCI ones, so a real integration needs this second layer.
    providers: dict[str, dict[str, CauseClass]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> CodeMap:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        mappings: dict[str, CodeMapping] = {}
        emits: dict[tuple[Rail, CauseClass], str] = {}
        for code, m in raw["codes"].items():
            rail, cause = Rail(m["rail"]), CauseClass(m["cause"])
            mappings[code] = CodeMapping(
                code=code, rail=rail, cause=cause, retryable=m["retryable"],
                customer_action=m["customer_action"],
                terminal_for_rail=m["terminal_for_rail"], description=m["description"])
            if m.get("emits"):
                key = (rail, cause)
                if key in emits:
                    raise ValueError(
                        f"codemap: {rail.value}/{cause.value} emits both {emits[key]} "
                        f"and {code}; exactly one code per (rail, cause) may emit")
                emits[key] = code
        for code, m in raw.get("self_inflicted", {}).items():
            mappings[code] = CodeMapping(
                code=code, rail=Rail.ENACH, cause=CauseClass(m["cause"]), retryable=False,
                customer_action=False, terminal_for_rail=False,
                description=m["description"])
        providers = {
            name: {reason: CauseClass(cause) for reason, cause in table.items()}
            for name, table in (raw.get("providers") or {}).items()}
        return cls(version=raw["version"], mappings=mappings, emits=emits,
                   providers=providers)

    # ---- Layer 1: the direction the diagnoser reads ---------------------------

    def cause_of(self, code: str) -> CauseClass:
        """Unmapped codes are UNKNOWN. A taxonomy that silently swallows unknowns is how
        the drift problem hides — callers count them via `is_unmapped`."""
        m = self.mappings.get(code)
        return m.cause if m else CauseClass.UNKNOWN

    def is_unmapped(self, code: str) -> bool:
        return code not in self.mappings and code != SUCCESS

    def get(self, code: str) -> CodeMapping | None:
        return self.mappings.get(code)

    # ---- the direction the simulator reads -----------------------------------

    def code_for(self, rail: Rail, cause: CauseClass) -> str:
        """What this rail returns when this is what went wrong."""
        try:
            return self.emits[(rail, cause)]
        except KeyError:
            raise KeyError(f"codemap has no code for {rail.value}/{cause.value}") from None

    def provider_cause(self, provider: str, reason: str) -> CauseClass:
        """A live provider's own reason string, mapped onto our taxonomy.

        Unlisted reasons fall to UNKNOWN rather than guessing — which is what makes the
        drift visible on the scoreboard instead of silently mis-diagnosed.
        """
        return self.providers.get(provider, {}).get(reason, CauseClass.UNKNOWN)

    def causes_on(self, rail: Rail) -> frozenset[CauseClass]:
        return frozenset(c for (r, c) in self.emits if r == rail)


@lru_cache(maxsize=4)
def load_codemap(path: str | Path = DEFAULT_PATH) -> CodeMap:
    return CodeMap.load(path)
