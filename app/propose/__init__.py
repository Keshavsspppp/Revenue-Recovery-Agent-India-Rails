"""The proposer layer. It proposes; a deterministic executor disposes.

`RulesProposer` is the control arm and always available. `GroqProposer` is the treatment,
and whether it adds anything is measured in `app/eval/ablate.py` rather than assumed.
"""

from app.propose.groq import GroqProposer, ProposerUnavailable
from app.propose.rules import PRIORITY, RulesProposer
from app.propose.schema import (
    FORBIDDEN_KEYS,
    SYSTEM_PROMPT,
    Proposal,
    parse,
    response_format,
)

__all__ = ["FORBIDDEN_KEYS", "PRIORITY", "Proposal", "ProposerUnavailable",
           "GroqProposer", "RulesProposer", "SYSTEM_PROMPT", "parse", "response_format"]
