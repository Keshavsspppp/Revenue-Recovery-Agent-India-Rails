"""The scoreboard, computed from the ledger and nothing else.

Everything here reads raw events. The report is a *view*, never a separate accounting —
`tests/test_evaluator.py::test_report_matches_ledger` recomputes the headline figures with
independent SQL and requires them to agree.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from app.domain.codemap import NOTICE_WINDOW_VIOLATION, load_codemap
from app.domain.enums import ActionType, Arm, CauseClass, Stage
from app.domain.money import amount_band

#: Excluded from the "addressable" denominator: no intervention reaches them.
UNADDRESSABLE = frozenset({CauseClass.ACCOUNT_TERMINAL, CauseClass.MANDATE_REVOKED})

#: A contact in the harm sense. The pre-debit notice is mandatory and priced low, but it
#: still lands on the customer's phone, so it counts here.
CONTACT_LIKE = frozenset({ActionType.SEND_PREDEBIT_NOTICE.value,
                          ActionType.SEND_MESSAGE.value,
                          ActionType.SEND_PAYMENT_LINK.value,
                          ActionType.REQUEST_PTP.value,
                          ActionType.VOICE_CONFIRM_PTP.value})


@dataclass(frozen=True)
class AccountRow:
    """One account, folded out of its events. The unit of resampling."""

    account_id: str
    arm: Arm
    amount_paise: int
    cause: CauseClass
    band: str
    category: str
    city_tier: int
    settled: bool
    settled_source: str | None
    cost_paise: int
    contacts: int
    attempts: int
    opted_out: bool
    complained: bool
    disputed: bool
    mandate_cancelled: bool
    terminal_state: str
    first_failure_code: str
    days_to_recover: float | None

    @property
    def addressable(self) -> bool:
        return self.cause not in UNADDRESSABLE

    @property
    def recovered_paise(self) -> int:
        return self.amount_paise if self.settled else 0


@dataclass(frozen=True)
class ArmMetrics:
    arm: str
    accounts: int
    at_risk_paise: int
    recovered_paise: int
    cost_paise: int
    contacts: int
    attempts: int
    recovered_accounts: int

    @property
    def rate(self) -> float:
        return self.recovered_paise / self.at_risk_paise if self.at_risk_paise else 0.0


def load_rows(path: str | Path) -> list[AccountRow]:
    """Fold the ledger into one row per account. No world object, no simulator.

    Cached on the file's size and mtime. The ledger is append-only, so any write moves
    both — a dry-run gate decision from the demo endpoint invalidates the cache exactly
    as it should, and a page that asks twice does not re-read 125,000 events twice.
    """
    stat = Path(path).stat()
    return list(_load_rows_cached(str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=8)
def _load_rows_cached(path: str, _mtime_ns: int, _size: int) -> tuple[AccountRow, ...]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    codemap = load_codemap()

    meta = {r["account_id"]: r for r in con.execute(
        "SELECT c.account_id, c.amount_paise, c.first_failure_code,"
        " a.merchant_category, a.city_tier"
        " FROM cycles c JOIN accounts a ON a.account_id = c.account_id")}

    arms = {r["account_id"]: Arm(r["arm"]) for r in con.execute(
        "SELECT account_id, arm FROM events WHERE stage=? AND account_id IS NOT NULL",
        (Stage.ASSIGN.value,))}

    costs: dict[str, int] = {}
    contacts: dict[str, int] = {}
    for r in con.execute(
            "SELECT account_id, action_type, payload FROM events WHERE stage=?",
            (Stage.EXECUTE.value,)):
        payload = json.loads(r["payload"])
        costs[r["account_id"]] = costs.get(r["account_id"], 0) + int(
            (payload.get("result") or {}).get("cost_paise") or 0)
        if r["action_type"] in CONTACT_LIKE:
            contacts[r["account_id"]] = contacts.get(r["account_id"], 0) + 1

    detected = {r["account_id"]: r["occurred_at"] for r in con.execute(
        "SELECT account_id, occurred_at FROM events WHERE stage=?", (Stage.DETECT.value,))}

    terminals = {r["account_id"]: json.loads(r["payload"])["result"]["terminal_state"]
                 for r in con.execute(
                     "SELECT account_id, payload FROM events WHERE stage=?",
                     (Stage.CLOSE.value,))}

    rows: list[AccountRow] = []
    for r in con.execute(
            "SELECT account_id, payload FROM events WHERE stage=? AND settled IS NOT NULL",
            (Stage.OBSERVE.value,)):
        account_id = r["account_id"]
        result = json.loads(r["payload"])["result"]
        m = meta[account_id]
        rows.append(AccountRow(
            account_id=account_id,
            arm=arms[account_id],
            amount_paise=m["amount_paise"],
            cause=codemap.cause_of(m["first_failure_code"]),
            band=amount_band(m["amount_paise"]),
            category=m["merchant_category"],
            city_tier=m["city_tier"],
            settled=bool(result.get("ok")),
            settled_source=result.get("settled_source"),
            cost_paise=costs.get(account_id, 0),
            contacts=contacts.get(account_id, 0),
            attempts=int(result.get("attempts") or 0),
            opted_out=bool(result.get("opted_out")),
            complained=bool(result.get("complained")),
            disputed=bool(result.get("disputed")),
            mandate_cancelled=bool(result.get("mandate_cancelled")),
            terminal_state=terminals.get(account_id, ""),
            first_failure_code=m["first_failure_code"],
            days_to_recover=_days_between(detected.get(account_id),
                                          result.get("settled_at"))))
    con.close()
    return tuple(rows)


def _days_between(detected_at: str | None, settled_at: str | None) -> float | None:
    """Settlement is the clock that counts. An accepted request is not a recovery."""
    if not detected_at or not settled_at:
        return None
    from datetime import datetime as _dt
    delta = _dt.fromisoformat(settled_at) - _dt.fromisoformat(detected_at)
    return delta.total_seconds() / 86400.0


# ---- arm-level aggregates -----------------------------------------------------

def arm_metrics(rows: Iterable[AccountRow], arm: Arm) -> ArmMetrics:
    subset = [r for r in rows if r.arm is arm]
    return ArmMetrics(
        arm=arm.value,
        accounts=len(subset),
        at_risk_paise=sum(r.amount_paise for r in subset),
        recovered_paise=sum(r.recovered_paise for r in subset),
        cost_paise=sum(r.cost_paise for r in subset),
        contacts=sum(r.contacts for r in subset),
        attempts=sum(r.attempts for r in subset),
        recovered_accounts=sum(1 for r in subset if r.settled))


@dataclass
class Scoreboard:
    treatment: ArmMetrics
    holdout: ArmMetrics
    incremental_rate: float
    incremental_recovered_paise: int
    holdout_recovered_rate_adjusted_paise: int
    cost_delta_paise: int
    net_incremental_paise: int
    ci95: tuple[int, int]
    rate_ci95: tuple[float, float]
    denominators: dict[str, tuple[float, int]]
    harm: dict[str, dict[str, float]]
    compliance: dict[str, Any]
    terminals: dict[str, int]
    diagnostics: dict[str, Any]
    segments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def scoreboard(rows: Sequence[AccountRow], *, bootstrap_n: int = 10_000,
               bootstrap_seed: int = 7) -> Scoreboard:
    t = arm_metrics(rows, Arm.TREATMENT)
    h = arm_metrics(rows, Arm.HOLDOUT)

    incremental_rate = t.rate - h.rate
    incremental_recovered = int(incremental_rate * t.at_risk_paise)

    # The holdout arm also spends money — notices and default retries are not free — so
    # the comparison is between two policies, not between a policy and nothing. Scale the
    # holdout's cost onto the treatment arm's at-risk base before differencing, exactly as
    # its recovery is scaled. Differencing raw totals would compare a 1,600-account bill
    # against a 400-account one.
    scale = (t.at_risk_paise / h.at_risk_paise) if h.at_risk_paise else 0.0
    cost_delta = int(t.cost_paise - h.cost_paise * scale)
    net_incremental = incremental_recovered - cost_delta

    lo, hi = bootstrap_incremental(rows, n=bootstrap_n, seed=bootstrap_seed)
    return Scoreboard(
        treatment=t, holdout=h,
        incremental_rate=incremental_rate,
        incremental_recovered_paise=incremental_recovered,
        holdout_recovered_rate_adjusted_paise=int(h.rate * t.at_risk_paise),
        cost_delta_paise=cost_delta,
        net_incremental_paise=net_incremental,
        ci95=(lo, hi),
        rate_ci95=rate_difference_ci(rows),
        denominators=denominators(rows),
        harm={"treatment": harm_counters(rows, Arm.TREATMENT),
              "holdout": harm_counters(rows, Arm.HOLDOUT)},
        compliance=compliance_counters(rows),
        terminals=terminal_distribution(rows),
        diagnostics=diagnostics(rows, t, h),
        segments={"cause": segment(rows, lambda r: r.cause.value),
                  "amount": segment(rows, lambda r: r.band),
                  "category": segment(rows, lambda r: r.category),
                  "tier": segment(rows, lambda r: f"tier_{r.city_tier}")})


# ---- the three denominators ---------------------------------------------------

def denominators(rows: Sequence[AccountRow]) -> dict[str, tuple[float, int]]:
    """Recovery rate is a fraction and the denominator is a choice. Vendors quote the
    flattering one, so publish all three with the denominator printed next to each."""
    treatment = [r for r in rows if r.arm is Arm.TREATMENT]
    return {
        "per_retried": _rate([r for r in treatment if r.attempts > 0]),
        "per_addressable": _rate([r for r in treatment if r.addressable]),
        "per_all_failed": _rate(treatment),
    }


def _rate(subset: Sequence[AccountRow]) -> tuple[float, int]:
    """Numerator and denominator must describe the *same* accounts.

    Crediting all recovered value against a restricted denominator is how a recovery rate
    ends up above 100% — which this reported, briefly, before the per-retried denominator
    stopped counting accounts that self-paid without ever being attempted.
    """
    base = sum(r.amount_paise for r in subset)
    recovered = sum(r.recovered_paise for r in subset)
    return ((recovered / base) if base else 0.0, len(subset))


# ---- confidence intervals ------------------------------------------------------

def rate_difference_ci(rows: Sequence[AccountRow]) -> tuple[float, float]:
    """Two-proportion normal approximation on *account* recovery — safe because it is a
    proportion. The value-weighted lift needs the bootstrap below instead."""
    t = [r for r in rows if r.arm is Arm.TREATMENT]
    h = [r for r in rows if r.arm is Arm.HOLDOUT]
    if not t or not h:
        return (0.0, 0.0)
    p_t = sum(r.settled for r in t) / len(t)
    p_h = sum(r.settled for r in h) / len(h)
    se = math.sqrt(p_t * (1 - p_t) / len(t) + p_h * (1 - p_h) / len(h))
    return (p_t - p_h - 1.96 * se, p_t - p_h + 1.96 * se)


#: Strata thinner than this are pooled before resampling. A stratum of one account
#: resamples to itself every time and contributes *zero* variance, so fine-grained strata
#: silently narrow the interval and manufacture significance. Found by the A/A test in
#: tests/test_evaluator.py, which is exactly what that test is for.
MIN_STRATUM = 25


def bootstrap_incremental(rows: Sequence[AccountRow], n: int = 10_000, seed: int = 7,
                          min_stratum: int = MIN_STRATUM) -> tuple[int, int]:
    """Stratified bootstrap over accounts, resampling within (arm x cause x band).

    Amounts are heavy-tailed — a Rs 42,000 EMI and a Rs 299 subscription sit in the same
    batch — so the normal approximation is unsafe on the value-weighted lift. Report the
    interval even when it straddles zero: that is an honest result, and claiming
    significance you do not have is the fastest way to lose a knowledgeable room.

    Cells below `min_stratum` are pooled within their arm. Without that the interval is
    correct on 2,000 accounts and badly too narrow on 800, because the cells thin out
    faster than the batch does.
    """
    if n <= 0 or not rows:
        return (0, 0)
    rng = np.random.default_rng(seed)

    counts: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r.arm.value, r.cause.value, r.band)
        counts[key] = counts.get(key, 0) + 1

    strata: dict[tuple[str, str, str], list[AccountRow]] = {}
    for r in rows:
        key = (r.arm.value, r.cause.value, r.band)
        if counts[key] < min_stratum:
            key = (r.arm.value, "_pooled", "_pooled")
        strata.setdefault(key, []).append(r)

    keys = sorted(strata)
    amounts = {k: np.array([r.amount_paise for r in strata[k]], dtype=np.int64)
               for k in keys}
    recovered = {k: np.array([r.recovered_paise for r in strata[k]], dtype=np.int64)
                 for k in keys}

    # One draw per stratum for all `n` resamples at once. The nested Python loop this
    # replaces was the whole cost of the endpoint — 7.6s for a scoreboard, which reads on
    # screen as a page that never finishes loading. Chunked so a large stratum cannot
    # allocate an n x size index array in one go.
    t_at_risk = np.zeros(n, dtype=np.float64)
    t_rec = np.zeros(n, dtype=np.float64)
    h_at_risk = np.zeros(n, dtype=np.float64)
    h_rec = np.zeros(n, dtype=np.float64)
    chunk = max(1, min(n, 2_000_000 // max(1, max(len(amounts[k]) for k in keys))))
    for k in keys:
        size = len(amounts[k])
        a_sum = np.empty(n, dtype=np.float64)
        r_sum = np.empty(n, dtype=np.float64)
        for lo in range(0, n, chunk):
            hi = min(n, lo + chunk)
            idx = rng.integers(0, size, (hi - lo, size))
            a_sum[lo:hi] = amounts[k][idx].sum(axis=1)
            r_sum[lo:hi] = recovered[k][idx].sum(axis=1)
        if k[0] == Arm.TREATMENT.value:
            t_at_risk += a_sum
            t_rec += r_sum
        else:
            h_at_risk += a_sum
            h_rec += r_sum

    rate_t = np.divide(t_rec, t_at_risk, out=np.zeros(n), where=t_at_risk > 0)
    rate_h = np.divide(h_rec, h_at_risk, out=np.zeros(n), where=h_at_risk > 0)
    draws = (rate_t - rate_h) * t_at_risk
    return (int(np.percentile(draws, 2.5)), int(np.percentile(draws, 97.5)))


# ---- harm, compliance, segments ------------------------------------------------

def harm_counters(rows: Sequence[AccountRow], arm: Arm) -> dict[str, float]:
    """Per thousand accounts. These belong on the same screen as the money, not on a
    separate ethics slide — a recovery number without its harm number is incomplete."""
    subset = [r for r in rows if r.arm is arm]
    n = len(subset) or 1
    per_1k = 1000.0 / n
    contacts = sorted(r.contacts for r in subset)
    return {
        "opt_outs_per_1k": sum(r.opted_out for r in subset) * per_1k,
        "complaints_per_1k": sum(r.complained for r in subset) * per_1k,
        "disputes_per_1k": sum(r.disputed for r in subset) * per_1k,
        "mandate_cancellations_per_1k": sum(r.mandate_cancelled for r in subset) * per_1k,
        "hardship_exits_per_1k": sum(r.terminal_state == "HARDSHIP" for r in subset) * per_1k,
        "contacts_p50": _percentile(contacts, 50),
        "contacts_p95": _percentile(contacts, 95),
        "contacts_per_recovery": (sum(r.contacts for r in subset)
                                  / max(1, sum(r.settled for r in subset))),
    }


def days_to_recover(rows: Sequence[AccountRow], arm: Arm) -> dict[str, float]:
    """Measured from DETECT to settlement. Speed is a real part of the value: money
    recovered on day 4 is worth more than the same money on day 27."""
    days = sorted(r.days_to_recover for r in rows
                  if r.arm is arm and r.days_to_recover is not None)
    return {"p50": _percentile(days, 50), "p90": _percentile(days, 90), "n": len(days)}


def _percentile(values: Sequence[int], pct: int) -> float:
    return float(np.percentile(values, pct)) if values else 0.0


def compliance_counters(rows: Sequence[AccountRow]) -> dict[str, Any]:
    codemap = load_codemap()
    return {
        "notice_window_violations": 0,     # filled by compliance_from_ledger
        "unmapped_code_count": sum(1 for r in rows
                                   if codemap.is_unmapped(r.first_failure_code)),
        "policy_denials_by_rule": {},
        "circuit_breaker_tripped": False,
    }


def compliance_from_ledger(path: str | Path, rows: Sequence[AccountRow],
                           ) -> dict[str, Any]:
    """Denials and self-inflicted defects, straight from the events."""
    con = sqlite3.connect(str(path))
    denials = dict(con.execute(
        "SELECT rule_failed, COUNT(*) FROM events WHERE stage=? AND rule_failed IS NOT NULL"
        " GROUP BY rule_failed ORDER BY 2 DESC", (Stage.GATE.value,)).fetchall())
    violations = con.execute(
        "SELECT COUNT(*) FROM events WHERE stage=? AND json_extract(payload,"
        " '$.result.rail_code')=?", (Stage.EXECUTE.value, NOTICE_WINDOW_VIOLATION)
    ).fetchone()[0]
    con.close()
    out = compliance_counters(rows)
    out["policy_denials_by_rule"] = denials
    out["notice_window_violations"] = violations
    return out


def terminal_distribution(rows: Sequence[AccountRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.terminal_state] = counts.get(r.terminal_state, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def segment(rows: Sequence[AccountRow], key) -> list[dict[str, Any]]:
    """Every metric, cut by cause / band / category / tier. This is where the real
    insight lives: the agent will crush some classes and do nothing for others."""
    groups: dict[str, list[AccountRow]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = []
    for name, members in sorted(groups.items()):
        t = arm_metrics(members, Arm.TREATMENT)
        h = arm_metrics(members, Arm.HOLDOUT)
        out.append({
            "segment": name,
            "accounts": len(members),
            "treatment_n": t.accounts, "holdout_n": h.accounts,
            "rate_treatment": t.rate, "rate_holdout": h.rate,
            "incremental_rate": t.rate - h.rate,
            "incremental_recovered_paise": int((t.rate - h.rate) * t.at_risk_paise),
            "at_risk_paise": t.at_risk_paise + h.at_risk_paise,
        })
    return out


def diagnostics(rows: Sequence[AccountRow], t: ArmMetrics, h: ArmMetrics) -> dict[str, Any]:
    """Report, do not headline."""
    self_pay = sum(1 for r in rows if r.settled_source == "self_pay")
    rail = sum(1 for r in rows if r.settled_source == "rail")
    return {
        # Say this number out loud before anyone asks for it.
        "self_cure_share": (h.rate / t.rate) if t.rate else 0.0,
        "holdout_rate": h.rate,
        "treatment_rate": t.rate,
        "recovered_by_self_pay": self_pay,
        "recovered_by_rail": rail,
        "recovered_by_self_pay_share": self_pay / max(1, self_pay + rail),
        "days_to_recover_treatment": days_to_recover(rows, Arm.TREATMENT),
        "days_to_recover_holdout": days_to_recover(rows, Arm.HOLDOUT),
    }
