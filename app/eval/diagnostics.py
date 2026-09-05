"""Diagnostics scored against ground truth. Report, do not headline.

These are only computable in simulation, and that is exactly why they are worth
producing: `latent_truth` is the answer sheet, and a detector nobody has scored is a
detector nobody should trust. The hardship number in particular is free to compute and
directly relevant to conduct — being wrong about who cannot pay is bad in both directions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.enums import Stage


@dataclass(frozen=True)
class Classification:
    """A confusion matrix and the three numbers everyone asks for."""

    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def base_rate(self) -> float:
        total = (self.true_positive + self.false_positive
                 + self.false_negative + self.true_negative)
        return (self.true_positive + self.false_negative) / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"precision": self.precision, "recall": self.recall, "f1": self.f1,
                "base_rate": self.base_rate, "true_positive": self.true_positive,
                "false_positive": self.false_positive,
                "false_negative": self.false_negative,
                "true_negative": self.true_negative}


def hardship_detector(path: str | Path, threshold: float | None = None) -> Classification:
    """Score the detector against `latent_truth.hardship`.

    Predicted positive means the agent actually routed the account out of recovery — the
    decision, not the score that led to it, because the decision is what reaches a person.
    """
    con = sqlite3.connect(str(path))
    truth = {a: bool(h) for a, h in con.execute(
        "SELECT account_id, hardship FROM latent_truth")}

    flagged: set[str] = set()
    for account_id, payload in con.execute(
            "SELECT account_id, payload FROM events WHERE stage=?", (Stage.CLOSE.value,)):
        if json.loads(payload)["result"].get("terminal_state") == "HARDSHIP":
            flagged.add(account_id)

    treated = {a for (a,) in con.execute(
        "SELECT account_id FROM events WHERE stage=? AND arm='treatment'",
        (Stage.ASSIGN.value,))}
    con.close()

    # Scored on the treatment arm only: the holdout never runs the detector, so counting
    # it would dilute the result with accounts that were never given a chance to be found.
    tp = sum(1 for a in treated if a in flagged and truth.get(a))
    fp = sum(1 for a in treated if a in flagged and not truth.get(a))
    fn = sum(1 for a in treated if a not in flagged and truth.get(a))
    tn = sum(1 for a in treated if a not in flagged and not truth.get(a))
    return Classification(tp, fp, fn, tn)


def cause_accuracy(path: str | Path) -> dict[str, Any]:
    """Layer 1 against the staged cause. A sanity check only — `04-CAUSE-TAXONOMY.md` is
    explicit that the metric that matters is the recovery rate of the actions a diagnosis
    triggers, not its classification accuracy."""
    from app.domain.codemap import load_codemap

    codemap = load_codemap()
    con = sqlite3.connect(str(path))
    rows = con.execute(
        "SELECT c.first_failure_code, l.true_cause FROM cycles c"
        " JOIN latent_truth l ON l.account_id = c.account_id").fetchall()
    con.close()
    if not rows:
        return {"accuracy": 0.0, "n": 0}
    correct = sum(1 for code, truth in rows
                  if truth and codemap.cause_of(code).value == truth)
    return {"accuracy": correct / len(rows), "n": len(rows),
            "note": "sanity check only; action_hit_rate is the metric that matters"}


def action_hit_rate(path: str | Path) -> dict[str, dict[str, Any]]:
    """P(recovery | the action this diagnosis triggered), by cause class.

    This is the honest way to score a cause model: you never have ground truth for "true
    cause" in production, but you always have it for "did the intervention work".
    """
    from app.domain.codemap import load_codemap

    codemap = load_codemap()
    con = sqlite3.connect(str(path))
    settled = {a for a, in con.execute(
        "SELECT account_id FROM events WHERE stage=? AND settled=1", (Stage.OBSERVE.value,))}
    causes = {a: codemap.cause_of(code).value for a, code in con.execute(
        "SELECT account_id, first_failure_code FROM cycles")}
    acted: dict[str, set[str]] = {}
    for account_id, action in con.execute(
            "SELECT account_id, action_type FROM events WHERE stage=? AND arm='treatment'",
            (Stage.EXECUTE.value,)):
        acted.setdefault(causes.get(account_id, "UNKNOWN"), set()).add(account_id)
    con.close()

    return {cause: {"acted_on": len(accounts),
                    "recovered": len(accounts & settled),
                    "hit_rate": len(accounts & settled) / len(accounts) if accounts else 0.0}
            for cause, accounts in sorted(acted.items())}
