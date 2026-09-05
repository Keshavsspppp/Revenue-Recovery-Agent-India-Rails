"""Rendering only. Every number here comes from `metrics.scoreboard`; nothing is
computed in this file. The report is a view over the ledger, never a second accounting.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.domain.money import format_inr
from app.eval.metrics import Scoreboard, compliance_from_ledger, load_rows, scoreboard


def build(path: str | Path, *, bootstrap_n: int = 10_000) -> tuple[Scoreboard, dict[str, Any]]:
    rows = load_rows(path)
    if not rows:
        raise SystemExit(f"{path} has no OBSERVE events — run `rr run` first")
    board = scoreboard(rows, bootstrap_n=bootstrap_n)
    board.compliance = compliance_from_ledger(path, rows)
    from app.eval.diagnostics import cause_accuracy, hardship_detector
    detector = hardship_detector(path)
    board.diagnostics["hardship_detector"] = detector.as_dict()
    board.diagnostics["cause_accuracy"] = cause_accuracy(path)
    con = sqlite3.connect(str(path))
    meta = con.execute("SELECT batch_id, seed, policy_version, lambda_harm, holdout_frac,"
                       " policy FROM batches LIMIT 1").fetchone()
    con.close()
    return board, {"batch_id": meta[0], "seed": meta[1], "policy_version": meta[2],
                   "lambda_harm": meta[3], "holdout_frac": meta[4],
                   "policy": meta[5] or "nothing"}


def render(board: Scoreboard, meta: dict[str, Any], segment: str | None = None) -> str:
    t, h = board.treatment, board.holdout
    at_risk = t.at_risk_paise + h.at_risk_paise
    lo, hi = board.ci95
    out: list[str] = []
    add = out.append

    add(f"BATCH {meta['batch_id']}   seed={meta['seed']}   policy={meta['policy']}"
        f"   rules={meta['policy_version']}   λ={meta['lambda_harm']}   proposer=none")
    add(f"accounts {t.accounts + h.accounts} (treatment {t.accounts} / holdout {h.accounts})"
        f"   at-risk {format_inr(at_risk)}")
    add("")
    add("  RECOVERY")
    add(f"    gross recovered (treatment)        {format_inr(t.recovered_paise):>16}"
        f"   {t.rate:>6.1%}")
    add(f"    holdout recovered (rate-adjusted)  "
        f"{format_inr(board.holdout_recovered_rate_adjusted_paise):>16}   {h.rate:>6.1%}"
        f"   <- self-cure baseline")
    add(f"    INCREMENTAL RECOVERED              "
        f"{format_inr(board.incremental_recovered_paise):>16}"
        f"   {board.incremental_rate:>6.1%}   95% CI "
        f"[{format_inr(lo)}, {format_inr(hi)}]")
    add(f"    cost of recovery (T - H adjusted)  {format_inr(board.cost_delta_paise):>16}")
    add(f"    NET INCREMENTAL                    "
        f"{format_inr(board.net_incremental_paise):>16}")
    if lo <= 0 <= hi:
        add("    note: the interval straddles zero. Reported as-is; see the per-cause cut")
        add("          below for where value is and is not being added.")
    add("")
    add("  RATES (denominator printed)")
    labels = {"per_retried": "per retried accounts       ",
              "per_addressable": "per addressable failures   ",
              "per_all_failed": "per all failed volume      "}
    for key, label in labels.items():
        rate, n = board.denominators[key]
        add(f"    {label}        {rate:>6.1%}   (n={n:,})")
    add("      addressable = failures excluding ACCOUNT_TERMINAL and MANDATE_REVOKED")
    add("")
    add("  EFFICIENCY")
    ht, hh = board.harm["treatment"], board.harm["holdout"]
    add(f"    contacts per recovery                 {ht['contacts_per_recovery']:.2f}")
    add(f"    attempts (treatment / holdout)        {t.attempts} / {h.attempts}")
    dt, dh = board.diagnostics["days_to_recover_treatment"], board.diagnostics["days_to_recover_holdout"]
    add(f"    days to recover (treatment)           p50 {dt['p50']:.1f}   p90 {dt['p90']:.1f}")
    add(f"    days to recover (holdout)             p50 {dh['p50']:.1f}   p90 {dh['p90']:.1f}")
    add("")
    add("  HARM (per 1,000 accounts)          treatment   holdout")
    for key, label in (("opt_outs_per_1k", "opt-outs"),
                       ("complaints_per_1k", "complaints"),
                       ("mandate_cancellations_per_1k", "mandate cancellations"),
                       ("disputes_per_1k", "disputes"),
                       ("hardship_exits_per_1k", "hardship exits")):
        add(f"    {label:<32} {ht[key]:>7.1f}   {hh[key]:>7.1f}")
    add(f"    {'contacts per account (p50/p95)':<32} "
        f"{ht['contacts_p50']:>3.0f}/{ht['contacts_p95']:<3.0f} "
        f"{hh['contacts_p50']:>5.0f}/{hh['contacts_p95']:<3.0f}")
    add("")
    add("  COMPLIANCE")
    denials = board.compliance["policy_denials_by_rule"]
    add("    policy denials      "
        + (" · ".join(f"{k} {v}" for k, v in denials.items()) if denials
           else "none (gate lands at M5)"))
    violations = board.compliance["notice_window_violations"]
    add(f"    notice window violations              {violations}"
        + ("   <- OUR BUG, not a customer failure" if violations else ""))
    add(f"    unmapped rail codes                   {board.compliance['unmapped_code_count']}")
    add(f"    circuit breaker                       "
        f"{'TRIPPED' if board.compliance['circuit_breaker_tripped'] else 'not tripped'}")
    add("")
    add("  TERMINAL STATES")
    add("    " + " · ".join(f"{k} {v}" for k, v in board.terminals.items()))
    add("")
    add("  DIAGNOSTICS (report, do not headline)")
    d = board.diagnostics
    add(f"    self-cure share (holdout/treatment)   {d['self_cure_share']:.3f}")
    add(f"    recovered by rail / by self-pay       {d['recovered_by_rail']}"
        f" / {d['recovered_by_self_pay']}"
        f"   ({d['recovered_by_self_pay_share']:.1%} self-pay)")
    hd = d.get("hardship_detector")
    if hd:
        add(f"    hardship detector                     precision {hd['precision']:.2f}"
            f"   recall {hd['recall']:.2f}   F1 {hd['f1']:.2f}"
            f"   (base rate {hd['base_rate']:.1%})")
        add(f"      scored against latent_truth: {hd['true_positive']} found, "
            f"{hd['false_positive']} wrongly flagged, {hd['false_negative']} missed")
    ca = d.get("cause_accuracy")
    if ca:
        add(f"    cause accuracy (sanity check only)    {ca['accuracy']:.3f}"
            f"   n={ca['n']}")

    if segment:
        add("")
        add(f"  SEGMENT: {segment}")
        add(f"    {'segment':<22} {'n':>6} {'rate T':>8} {'rate H':>8} {'incr':>8}"
            f" {'incr value':>16}")
        for row in board.segments[segment]:
            add(f"    {row['segment']:<22} {row['accounts']:>6} "
                f"{row['rate_treatment']:>7.1%} {row['rate_holdout']:>7.1%} "
                f"{row['incremental_rate']:>7.1%} "
                f"{format_inr(row['incremental_recovered_paise']):>16}")
    return "\n".join(out)


def as_json(board: Scoreboard, meta: dict[str, Any]) -> str:
    payload = {
        "batch": meta,
        "treatment": asdict(board.treatment),
        "holdout": asdict(board.holdout),
        "incremental_rate": board.incremental_rate,
        "incremental_recovered_paise": board.incremental_recovered_paise,
        "incremental_recovered_display": format_inr(board.incremental_recovered_paise),
        "holdout_recovered_rate_adjusted_paise": board.holdout_recovered_rate_adjusted_paise,
        "cost_delta_paise": board.cost_delta_paise,
        "net_incremental_paise": board.net_incremental_paise,
        "net_incremental_display": format_inr(board.net_incremental_paise),
        "ci95_paise": list(board.ci95),
        "rate_ci95": list(board.rate_ci95),
        "denominators": {k: {"rate": v[0], "n": v[1]}
                         for k, v in board.denominators.items()},
        "harm": board.harm,
        "compliance": board.compliance,
        "terminals": board.terminals,
        "diagnostics": board.diagnostics,
        "segments": board.segments,
    }
    return json.dumps(payload, indent=2)
