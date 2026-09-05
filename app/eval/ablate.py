"""The three-way proposer comparison. docs/07-PROPOSER-GROQ.md §Ablation.

Identical batch, identical seed, identical policy version, three proposers:

    A  rules            a deterministic heuristic over the eligible set. No MDP.
    B  planner-argmax   the budgeted MDP, no proposer layer.
    C  groq             the LLM proposes, the planner falls back.

A -> B measures what the MDP adds over a sensible heuristic. B -> C measures what the LLM
adds over the MDP. If C is inside B's interval, say so plainly and ship B: the measurement
infrastructure that let you find that out is the achievement, and it is what separates
this from a demo with an LLM bolted to a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain.config import Config
from app.domain.enums import Arm
from app.eval.metrics import arm_metrics, bootstrap_incremental, load_rows
from app.propose import GroqProposer, ProposerUnavailable, RulesProposer

ARMS = ("rules", "planner-argmax", "groq")


@dataclass
class AblationRow:
    name: str
    available: bool
    accounts: int = 0
    treatment_rate: float = 0.0
    holdout_rate: float = 0.0
    incremental_paise: int = 0
    ci95: tuple[int, int] = (0, 0)
    cost_paise: int = 0
    contacts: int = 0
    attempts: int = 0
    recovered_accounts: int = 0
    agreement_with_planner: float | None = None
    consulted: int = 0
    proposer_stats: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _proposer_for(name: str):
    """`planner-argmax` is the absence of a proposer, not a kind of one."""
    if name == "rules":
        return RulesProposer()
    if name == "planner-argmax":
        return None
    if name == "groq":
        return GroqProposer.from_env()
    raise ValueError(name)


def run(batch_dir: str | Path, cfg: Config, *, seed: int, n_accounts: int,
        holdout_frac: float | None = None, bootstrap_n: int = 4000,
        arms: tuple[str, ...] = ARMS) -> list[AblationRow]:
    """One freshly simulated batch per arm, from the same seed — so the world, the arm
    assignment and the holdout are identical and only the proposer differs."""
    from app.runner import run_batch
    from app.sim.generate import simulate_batch

    root = Path(batch_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows: list[AblationRow] = []

    for name in arms:
        try:
            proposer = _proposer_for(name)
        except ProposerUnavailable as e:
            rows.append(AblationRow(name=name, available=False, note=str(e)))
            continue

        db = root / f"ablate_{name.replace(':', '_')}.db"
        # A fresh batch per arm is the point. Simulating over a previous run's file
        # collides on event_id and dies inside the ledger, which reads as a ledger bug
        # rather than as "you ran this twice".
        db.unlink(missing_ok=True)
        path = str(db)
        simulate_batch(cfg, seed=seed, n_accounts=n_accounts, out_path=path)
        run_batch(path, cfg, policy="agent", holdout_frac=holdout_frac,
                  proposer=proposer)

        data = load_rows(path)
        t = arm_metrics(data, Arm.TREATMENT)
        h = arm_metrics(data, Arm.HOLDOUT)
        lo, hi = bootstrap_incremental(data, n=bootstrap_n)
        rows.append(AblationRow(
            name=name, available=True, accounts=len(data),
            treatment_rate=t.rate, holdout_rate=h.rate,
            incremental_paise=int((t.rate - h.rate) * t.at_risk_paise),
            ci95=(lo, hi), cost_paise=t.cost_paise, contacts=t.contacts,
            attempts=t.attempts, recovered_accounts=t.recovered_accounts,
            **dict(zip(("agreement_with_planner", "consulted"), _agreement(path))),
            proposer_stats=proposer.stats() if hasattr(proposer, "stats") else {}))
    return rows


def _agreement(path: str) -> tuple[float | None, int]:
    """How often the proposer picked what the planner would have picked anyway.

    A high agreement rate with no measurable lift is the most useful negative result the
    ablation can produce: it says the proposer is an expensive way to reach the same
    answer.
    """
    import json
    import sqlite3

    from app.domain.enums import Stage

    con = sqlite3.connect(path)
    total = agreed = 0
    for (payload,) in con.execute("SELECT payload FROM events WHERE stage=?",
                                  (Stage.PROPOSE.value,)):
        event = json.loads(payload)
        model = event.get("model") or {}
        if model.get("proposer") in (None, "planner-argmax"):
            continue
        # Only decisions the proposer actually answered. `consulted` says the margin
        # gate opened; it does not say a proposal came back. Counting the fall-throughs
        # scores the planner against itself, and they agree by construction — on the run
        # that hit a daily token limit that inflated 95.3% to 98.3%.
        if not model.get("consulted") or model.get("fell_back"):
            continue
        total += 1
        # The planner's own pick is the top-ranked q: line in the evidence.
        qs = [e for e in (event.get("evidence") or ()) if e.startswith("q:")]
        chosen = (event.get("action") or {}).get("type")
        if qs and chosen and qs[0].split(":")[1] == chosen:
            agreed += 1
    con.close()
    return ((agreed / total) if total else None), total


def render(rows: list[AblationRow], cfg: Config) -> str:
    from app.domain.money import format_inr

    out: list[str] = []
    add = out.append
    add(f"PROPOSER ABLATION   policy=agent   rules={cfg.policy_version}   "
        f"λ={cfg.lambda_harm}")
    add("")
    add(f"  {'arm':<16} {'recovery':>9} {'incremental':>16} {'95% CI':>34} "
        f"{'cost':>11} {'contacts':>9}")
    for r in rows:
        if not r.available:
            add(f"  {r.name:<16} unavailable — {r.note.splitlines()[0]}")
            continue
        add(f"  {r.name:<16} {r.treatment_rate:>8.1%} "
            f"{format_inr(r.incremental_paise):>16} "
            f"[{format_inr(r.ci95[0])}, {format_inr(r.ci95[1])}]".rjust(34)
            + f" {format_inr(r.cost_paise):>11} {r.contacts:>9}")

    live = [r for r in rows if r.available]
    if len(live) >= 2:
        add("")
        add("  READ THIS AS")
        base = {r.name: r for r in live}
        if "rules" in base and "planner-argmax" in base:
            delta = (base["planner-argmax"].incremental_paise
                     - base["rules"].incremental_paise)
            add(f"    the MDP over a heuristic          {format_inr(delta):>16}")
        if "planner-argmax" in base and "groq" in base:
            delta = base["groq"].incremental_paise - base["planner-argmax"].incremental_paise
            add(f"    the LLM over the MDP              {format_inr(delta):>16}")
            overlap = _intervals_overlap(base["groq"].ci95, base["planner-argmax"].ci95)
            if overlap:
                add("    the two intervals overlap, so this difference is not measurable")
                add("    on this batch. The honest move is to ship the planner and say so.")
    for r in live:
        if r.proposer_stats:
            add("")
            add(f"  {r.name} calls={r.proposer_stats.get('calls', 0)} "
                f"cache_hits={r.proposer_stats.get('cache_hits', 0)} "
                f"invalid={r.proposer_stats.get('invalid', 0)} "
                f"failures={r.proposer_stats.get('failures', 0)} "
                f"latency p50={r.proposer_stats.get('latency_p50_ms', 0)}ms "
                f"p95={r.proposer_stats.get('latency_p95_ms', 0)}ms")
            # Without the breakdown a failure count is unreadable: a model that declined
            # and a transport that was rate-limited produce the same number and mean
            # opposite things. This is the line that stops the ablation being a lie.
            kinds = r.proposer_stats.get("failure_kinds") or {}
            if kinds:
                add("  " + " " * len(r.name) + " failures by kind: "
                    + " ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
            if r.proposer_stats.get("exhausted"):
                add("")
                add(f"  !! {r.name} HIT A PROVIDER DAILY LIMIT PART-WAY THROUGH.")
                add(f"     {r.proposer_stats['exhausted']}")
                add("     Every call after that fell through to the planner, so this row")
                add("     is part model and part planner and is NOT a measurement of the")
                add("     proposer. Re-run it with budget or do not quote it.")
        if r.agreement_with_planner is not None:
            add(f"  {r.name} agreed with the planner's own pick "
                f"{r.agreement_with_planner:.1%} of {r.consulted} decisions it was "
                f"actually asked about")
    return "\n".join(out)


def _intervals_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]
