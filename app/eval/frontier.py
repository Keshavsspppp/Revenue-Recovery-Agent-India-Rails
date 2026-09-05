"""The lambda frontier: net recovery against harm, as a dial rather than an assertion.

Re-solve the planner at several harm prices over the identical world and arm assignment,
and plot what each one buys. The sentence this exists to make sayable is:

    "At lambda=0 we recover X with N opt-outs per thousand. At lambda=1, X-4% with half
     as many. Here is the dial, and here is why we shipped it where we did."

That demonstrates harm was *priced*, not mentioned. Solving takes milliseconds, so the
only cost is re-running the batch — which is why this is cheap enough to be a standing
artefact rather than a one-off chart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.config import Config
from app.domain.enums import Arm

DEFAULT_LAMBDAS = (0.0, 0.25, 0.5, 1.0, 2.0)


@dataclass
class FrontierPoint:
    lambda_harm: float
    treatment_rate: float
    holdout_rate: float
    incremental_paise: int
    net_incremental_paise: int
    ci95: tuple[int, int]
    cost_paise: int
    contacts: int
    attempts: int
    opt_outs_per_1k: float
    complaints_per_1k: float
    mandate_cancellations_per_1k: float
    stopped_early: int

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def run(out_dir: str | Path, cfg: Config, *, seed: int, n_accounts: int,
        lambdas: tuple[float, ...] = DEFAULT_LAMBDAS,
        holdout_frac: float | None = None,
        bootstrap_n: int = 4000) -> list[FrontierPoint]:
    """One batch per lambda, same seed throughout.

    `lambda_harm` is excluded from the world hash precisely so this can run: it changes
    what the agent chooses, never what the customers do, so the holdout is identical at
    every point and the frontier measures the dial rather than the batch.
    """
    from app.eval.metrics import arm_metrics, load_rows, scoreboard
    from app.runner import run_batch
    from app.sim.generate import simulate_batch

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    points: list[FrontierPoint] = []

    for lam in lambdas:
        run_cfg = Config.load(lambda_harm=lam)
        path = str(root / f"frontier_lambda_{lam}.db")
        simulate_batch(run_cfg, seed=seed, n_accounts=n_accounts, out_path=path)
        run_batch(path, run_cfg, policy="agent", holdout_frac=holdout_frac)

        rows = load_rows(path)
        board = scoreboard(rows, bootstrap_n=bootstrap_n)
        t = arm_metrics(rows, Arm.TREATMENT)
        h = arm_metrics(rows, Arm.HOLDOUT)
        harm = board.harm["treatment"]
        treatment = [r for r in rows if r.arm is Arm.TREATMENT]
        points.append(FrontierPoint(
            lambda_harm=lam,
            treatment_rate=t.rate, holdout_rate=h.rate,
            incremental_paise=board.incremental_recovered_paise,
            net_incremental_paise=board.net_incremental_paise,
            ci95=board.ci95, cost_paise=t.cost_paise,
            contacts=t.contacts, attempts=t.attempts,
            opt_outs_per_1k=harm["opt_outs_per_1k"],
            complaints_per_1k=harm["complaints_per_1k"],
            mandate_cancellations_per_1k=harm["mandate_cancellations_per_1k"],
            stopped_early=sum(1 for r in treatment
                              if r.terminal_state == "EV_BELOW_THRESHOLD")))
    return points


def render(points: list[FrontierPoint], cfg: Config) -> str:
    from app.domain.money import format_inr

    out: list[str] = []
    add = out.append
    add(f"LAMBDA FRONTIER   policy=agent   rules={cfg.policy_version}")
    add("")
    add(f"  {'λ':>5} {'recovery':>9} {'net incremental':>17} {'contacts':>9} "
        f"{'attempts':>9} {'opt-outs/1k':>12} {'stopped':>8}")
    for p in points:
        add(f"  {p.lambda_harm:>5} {p.treatment_rate:>8.1%} "
            f"{format_inr(p.net_incremental_paise):>17} {p.contacts:>9} "
            f"{p.attempts:>9} {p.opt_outs_per_1k:>12.1f} {p.stopped_early:>8}")

    if len(points) >= 2:
        add("")
        add("  READ THIS AS")
        first, last = points[0], points[-1]
        add(f"    at λ={first.lambda_harm} the agent makes {first.contacts} contacts "
            f"for {format_inr(first.net_incremental_paise)}")
        add(f"    at λ={last.lambda_harm} it makes {last.contacts} "
            f"for {format_inr(last.net_incremental_paise)}")
        add(f"    shipped at λ={cfg.lambda_harm}")
        if not monotone_contacts(points):
            add("")
            add("    NOTE: contacts do not fall monotonically across these points. Harm is")
            add("    then not being priced as intended, and this plot should not be shown")
            add("    until that is understood — see tests/test_frontier.py.")
    add("")
    add("  Harm counters belong on the same screen as the money, which is why they are")
    add("  in this table rather than on a separate slide.")
    return "\n".join(out)


def monotone_contacts(points: list[FrontierPoint]) -> bool:
    """As the harm price rises, contact volume must fall. If it does not, harm is not
    actually priced and the frontier is decoration."""
    ordered = sorted(points, key=lambda p: p.lambda_harm)
    return all(a.contacts >= b.contacts for a, b in zip(ordered, ordered[1:]))
