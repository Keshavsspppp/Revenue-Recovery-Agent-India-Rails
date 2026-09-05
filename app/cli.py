"""`rr` — the CLI surface from docs/09-API.md. No business logic lives here.

Subcommands land as their milestone lands; unbuilt ones exit 2 with a pointer.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from app.domain.config import DEFAULT_PATH, Config
from app.domain.enums import Rail
from app.domain.money import format_inr
from app.ledger import Ledger


def _open(batch_path: str) -> Ledger:
    """Batch dbs hold exactly one batch; the id is in the batches table."""
    if not Path(batch_path).exists():
        raise SystemExit(f"no such batch: {batch_path}")
    con = sqlite3.connect(batch_path)
    row = con.execute("SELECT batch_id FROM batches LIMIT 1").fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"{batch_path} has no batch record — run `rr simulate` first")
    return Ledger(batch_path, row[0])


def cmd_verify(args: argparse.Namespace) -> int:
    """A build that cannot prove its own ledger is intact should not pass."""
    led = _open(args.batch)
    rep = led.verify(notice_hours=min(
        Config.load(args.config).notice_hours(r) for r in Rail if r is not Rail.PAYMENT_LINK))
    print(f"batch {led.batch_id}  events {rep.events}  ok={rep.ok}")
    if rep.first_bad_seq is not None:
        print(f"first bad seq: {rep.first_bad_seq}")
    for f in rep.failures[:20]:
        print(f"  FAIL {f}")
    if len(rep.failures) > 20:
        print(f"  ... {len(rep.failures) - 20} more")
    return 0 if rep.ok else 1


def cmd_timeline(args: argparse.Namespace) -> int:
    led = _open(args.batch)
    events = led.timeline(args.account)
    if not events:
        print(f"no events for {args.account}")
        return 1
    print(f"{'seq':>5}  {'occurred_at':<25} {'stage':<9} {'action':<22} rule_failed")
    for e in events:
        p = e.payload
        action = (p.get("action") or {}).get("type") or ""
        failed = (p.get("policy") or {}).get("check_failed") or ""
        print(f"{e.seq:>5}  {p['occurred_at']:<25} {p['stage']:<9} {action:<22} {failed}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from app.sim.generate import simulate_batch
    from app.sim.world import at_risk_paise

    cfg = Config.load(args.config, horizon_days=args.horizon)
    world, batch_id = simulate_batch(cfg, seed=args.seed, n_accounts=args.accounts,
                                     out_path=args.out)
    codes = Counter(s.first_failure_code for s in world.cycles.values())
    causes = Counter(cfg_codemap().cause_of(c).value for c in codes.elements())
    print(f"batch {batch_id}  seed {args.seed}  accounts {args.accounts}  "
          f"horizon {cfg.horizon_days}d")
    print(f"at-risk value {format_inr(at_risk_paise(world))}")
    print("first-failure cause mix (calibration target in docs/03-SIMULATOR.md):")
    total = sum(causes.values())
    for cause, n in causes.most_common():
        print(f"  {cause:<20} {n:>5}  {n / total:.3f}")
    print(f"written to {args.out}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from app.runner import AlreadyRun, ConfigDrift, run_batch

    cfg = Config.load(args.config, holdout_frac=args.holdout, lambda_harm=args.lambda_harm)
    proposer = None
    if args.proposer == "rules":
        from app.propose import RulesProposer
        proposer = RulesProposer()
    elif args.proposer == "groq":
        from app.propose import GroqProposer, ProposerUnavailable
        try:
            proposer = GroqProposer.from_env()
        except ProposerUnavailable as e:
            print(f"note: {e}", file=sys.stderr)
    try:
        meta = run_batch(args.batch, cfg, policy=args.policy,
                         holdout_frac=args.holdout, dry_run=args.dry_run,
                         proposer=proposer)
    except (AlreadyRun, ConfigDrift, NotImplementedError) as e:
        raise SystemExit(str(e))
    print(f"ran policy={args.policy} over {meta.n_accounts} accounts "
          f"(batch {meta.batch_id}, seed {meta.seed})")
    print(f"next: rr report --batch {args.batch}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from app.eval.report import as_json, build, render

    board, meta = build(args.batch)
    print(as_json(board, meta) if args.json else render(board, meta, args.segment))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    """The live-denial demo, from the terminal: set 19:30 and watch the gate refuse."""
    from datetime import datetime

    from app.domain.enums import ActionType, Channel, Rail
    from app.domain.models import Action
    from app.policy.evaluate_api import UnknownAccount, evaluate_hypothetical

    cfg = Config.load(args.config)
    action = Action(
        type=ActionType[args.action],
        channel=Channel[args.channel] if args.channel else None,
        rail=Rail[args.rail] if args.rail else None,
        template_id=args.template, cli_series=cfg.raw["policy"]["cli_series_service"],
        disclosure=True)
    try:
        out = evaluate_hypothetical(args.batch, args.account, action,
                                    datetime.fromisoformat(args.at), cfg)
    except UnknownAccount as e:
        raise SystemExit(str(e))

    print(f"{out['verdict']}" + (f" · {out['rule_id_failed']}" if out["rule_id_failed"] else ""))
    if out["reason"]:
        print(f"  {out['reason']}")
        print(f"  basis: {out['basis']}")
    print(f"  passed: {', '.join(out['rule_ids_passed']) or 'none'}")
    print(f"  policy_version: {out['policy_version']}   (written to the ledger, dry_run)")
    return 0 if out["verdict"] == "ALLOW" else 1


def cmd_rules(args: argparse.Namespace) -> int:
    from app.policy import catalogue, rules_hash

    print(f"{len(catalogue())} rules   rules_hash {rules_hash()}")
    for r in catalogue():
        print(f"  {r['rule_id']:<16} {r['title']}")
        print(f"  {'':<16} basis: {r['basis']}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Read back what Razorpay says happened to the objects this batch created."""
    from app.rails import RazorpayTestAdapter, RazorpayUnavailable
    from app.sync import render, sync_live

    cfg = Config.load(args.config)
    try:
        adapter = RazorpayTestAdapter.from_env(cfg)
    except RazorpayUnavailable as e:
        raise SystemExit(str(e))
    print(render(sync_live(args.batch, cfg, adapter), adapter))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Drive a small slice of a batch through Razorpay test mode."""
    from app.live import AlreadyClosed, render, run_live_slice
    from app.rails import RazorpayTestAdapter, RazorpayUnavailable

    cfg = Config.load(args.config)
    try:
        adapter = RazorpayTestAdapter.from_env(cfg)
    except RazorpayUnavailable as e:
        raise SystemExit(
            f"{e}\n\nAdd to .env:\n"
            "  RAZORPAY_KEY_ID=rzp_test_...\n"
            "  RAZORPAY_KEY_SECRET=...\n"
            "from the Razorpay dashboard under Settings -> API Keys, in Test Mode.")
    try:
        results = run_live_slice(args.batch, cfg, adapter, accounts=args.accounts,
                                 dry_run=args.dry_run)
    except AlreadyClosed as e:
        raise SystemExit(str(e))
    print(render(results, adapter))
    return 0


def cmd_frontier(args: argparse.Namespace) -> int:
    """Re-run the agent at several harm prices and print what each one buys."""
    import tempfile

    from app.eval.frontier import render, run

    cfg = Config.load(args.config)
    lambdas = tuple(float(x) for x in args.lambdas.split(","))
    points = run(args.out or tempfile.mkdtemp(), cfg, seed=args.seed,
                 n_accounts=args.accounts, lambdas=lambdas, holdout_frac=args.holdout)
    print(render(points, cfg))
    return 0


def cmd_ablate(args: argparse.Namespace) -> int:
    """rules / planner-argmax / groq over the identical batch and seed."""
    import tempfile

    from app.eval.ablate import render, run

    cfg = Config.load(args.config)
    rows = run(args.out or tempfile.mkdtemp(), cfg, seed=args.seed,
               n_accounts=args.accounts, holdout_frac=args.holdout)
    print(render(rows, cfg))
    return 0


def cfg_codemap():
    from app.domain.codemap import load_codemap
    return load_codemap()


def cmd_config(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    print(f"policy_version {cfg.policy_version}")
    print(f"config_hash    {cfg.config_hash}")
    print(f"horizon {cfg.horizon_days}d  holdout {cfg.holdout_frac}  lambda {cfg.lambda_harm}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rr", description="Revenue recovery agent — India rails")
    p.add_argument("--config", default=str(DEFAULT_PATH), help="path to config YAML")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("simulate", help="generate a batch of at-risk accounts")
    s.add_argument("--accounts", type=int, default=2000)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--horizon", type=int, default=None)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("run", help="run a policy over a batch")
    s.add_argument("--batch", required=True)
    s.add_argument("--policy", choices=["nothing", "fixed", "agent", "oracle"], default="agent")
    s.add_argument("--holdout", type=float, default=None)
    s.add_argument("--lambda", dest="lambda_harm", type=float, default=None)
    s.add_argument("--proposer", choices=["groq", "rules"], default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("report", help="print the scoreboard")
    s.add_argument("--batch", required=True)
    s.add_argument("--json", action="store_true")
    s.add_argument("--segment", choices=["cause", "amount", "category", "tier"])
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("verify", help="hash chain + ledger invariants")
    s.add_argument("--batch", required=True)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("timeline", help="audit trail for one account")
    s.add_argument("--batch", required=True)
    s.add_argument("--account", required=True)
    s.set_defaults(func=cmd_timeline)

    s = sub.add_parser("frontier", help="recovery/opt-out frontier over lambda")
    s.add_argument("--accounts", type=int, default=2000)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--holdout", type=float, default=None)
    s.add_argument("--lambdas", default="0,0.25,0.5,1,2")
    s.add_argument("--out", default=None, help="directory for the per-lambda batches")
    s.set_defaults(func=cmd_frontier)

    s = sub.add_parser("ablate", help="rules / planner-argmax / groq comparison")
    s.add_argument("--accounts", type=int, default=2000)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--holdout", type=float, default=None)
    s.add_argument("--out", default=None, help="directory for the per-arm batches")
    s.set_defaults(func=cmd_ablate)

    s = sub.add_parser("gate", help="evaluate an action against the gate, execute nothing")
    s.add_argument("--batch", required=True)
    s.add_argument("--account", required=True)
    s.add_argument("--action", default="VOICE_CONFIRM_PTP")
    s.add_argument("--channel", default="VOICE")
    s.add_argument("--rail", default=None)
    s.add_argument("--template", default="DLT_RECOVERY_PTP_001")
    s.add_argument("--at", required=True, help="simulated IST time, ISO-8601")
    s.set_defaults(func=cmd_gate)

    s = sub.add_parser("rules", help="print the rule catalogue and its hash")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("live", help="drive a slice of a batch through Razorpay test mode")
    s.add_argument("--batch", required=True)
    s.add_argument("--accounts", type=int, default=5)
    s.add_argument("--dry-run", action="store_true",
                   help="gate everything, call nothing")
    s.set_defaults(func=cmd_live)

    s = sub.add_parser("sync", help="read Razorpay back: which links were actually paid")
    s.add_argument("--batch", required=True)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("config", help="print the resolved config and its hash")
    s.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    from app.domain.env import load_dotenv

    # Every report prints rupees. A Windows console defaults to cp1252, which cannot
    # encode U+20B9, so `rr simulate` died in a traceback while formatting its own
    # success message. Say UTF-8 once, here, rather than degrading the money format.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
