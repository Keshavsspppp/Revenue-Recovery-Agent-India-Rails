"""Append-only, hash-chained event store. docs/02-LEDGER.md.

The write surface is `append()`. There is deliberately no update and no delete:
corrections are new events referencing the original `event_id`. Everything else here is
a read query or `verify()`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from app.domain.clock import wall_clock
from app.domain.enums import ActionType, Arm, Stage, Verdict
from app.domain.models import canonical_json, make_id, sha256_of

GENESIS = "sha256:genesis"

#: Invariant 5: the holdout arm runs the merchant default policy — a pre-debit notice and
#: scheduled retries — and nothing else. Any other action type there is a contaminated
#: control group, which silently inflates every downstream number.
MERCHANT_DEFAULT_ACTIONS = frozenset({
    ActionType.SEND_PREDEBIT_NOTICE.value,
    ActionType.RETRY_DEBIT.value,
})

DDL = """
PRAGMA journal_mode = WAL;
-- Every append commits, because a half-written audit trail is worse than a slow one.
-- WAL + NORMAL still survives an application crash; only an OS crash or power loss can
-- lose the last commits, and the batch is regenerable from its seed. FULL costs an fsync
-- per event (~2ms), which on a 20,000-event batch is a minute of the run spent waiting
-- on the disk rather than on anything the ledger guarantees.
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    prev_hash     TEXT NOT NULL,
    hash          TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    wall_clock_at TEXT NOT NULL,
    account_id    TEXT,
    cycle_id      TEXT,
    decision_id   TEXT,
    stage         TEXT NOT NULL,
    arm           TEXT,
    action_type   TEXT,
    action_hash   TEXT,
    rule_failed   TEXT,
    settled       INTEGER,
    amount_paise  INTEGER,
    payload       TEXT NOT NULL,
    UNIQUE (batch_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_account  ON events(batch_id, account_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_decision ON events(decision_id);
CREATE INDEX IF NOT EXISTS idx_events_stage    ON events(batch_id, stage);
CREATE INDEX IF NOT EXISTS idx_events_rule     ON events(batch_id, rule_failed);

CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    seed           INTEGER NOT NULL,
    config_json    TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    holdout_frac   REAL NOT NULL,
    lambda_harm    REAL NOT NULL,
    status         TEXT NOT NULL,
    policy         TEXT
);
"""


class LedgerError(Exception):
    """A refused write. The ledger rejects; it never repairs."""


@dataclass(frozen=True)
class EventDraft:
    """What a caller supplies. `event_id`, `seq`, `prev_hash` and `hash` are the ledger's
    to assign — that is what makes the chain the ledger's own claim rather than the
    caller's."""

    stage: Stage
    occurred_at: datetime
    account_id: str | None = None
    cycle_id: str | None = None
    decision_id: str | None = None
    arm: Arm | None = None
    action: dict[str, Any] | None = None
    action_hash: str | None = None
    cause_posterior: dict[str, float] | None = None
    evidence: tuple[str, ...] = ()
    policy: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    budgets_before: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    human_override: dict[str, Any] | None = None
    outcome_ref: str | None = None
    notice_ref: str | None = None   # the notice this debit consumes; invariant 8
    corrects: str | None = None     # event_id this event corrects — the only "edit"
    #: A hypothetical evaluated through /policy/evaluate. It executes nothing, reads no
    #: arm and authorises nothing — so it is exempt from the ASSIGN ordering rule, and
    #: `verify()` refuses any EXECUTE that tries to lean on one. A marker, not a note,
    #: because the verifier has to be able to check it.
    dry_run: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class Event:
    event_id: str
    seq: int
    prev_hash: str
    hash: str
    batch_id: str
    wall_clock_at: str
    payload: dict[str, Any]

    @property
    def stage(self) -> str:
        return self.payload["stage"]

    @property
    def account_id(self) -> str | None:
        return self.payload.get("account_id")

    @property
    def occurred_at(self) -> str:
        return self.payload["occurred_at"]


@dataclass
class VerifyReport:
    ok: bool = True
    events: int = 0
    first_bad_seq: int | None = None
    failures: list[str] = field(default_factory=list)

    def fail(self, msg: str, seq: int | None = None) -> None:
        self.ok = False
        self.failures.append(msg)
        if seq is not None and (self.first_bad_seq is None or seq < self.first_bad_seq):
            self.first_bad_seq = seq


class Ledger:
    """Single writer per batch."""

    def __init__(self, db_path: str | Path, batch_id: str) -> None:
        self.db_path = str(db_path)
        self.batch_id = batch_id
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        row = self.conn.execute(
            "SELECT seq, hash FROM events WHERE batch_id=? ORDER BY seq DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        self._seq = (row["seq"] + 1) if row else 0
        self._last_hash = row["hash"] if row else GENESIS
        self._assigned: set[str] = {
            r["account_id"]
            for r in self.conn.execute(
                "SELECT account_id FROM events WHERE batch_id=? AND stage=?",
                (batch_id, Stage.ASSIGN.value),
            )
        }
        self._closed_cycles: set[str] = {
            r["cycle_id"]
            for r in self.conn.execute(
                "SELECT cycle_id FROM events WHERE batch_id=? AND stage=?",
                (batch_id, Stage.CLOSE.value),
            )
            if r["cycle_id"]
        }

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- write surface -------------------------------------------------------

    def append(self, ev: EventDraft) -> Event:
        """Assigns seq, prev_hash, hash. Raises rather than writing a corrupt chain."""
        self._check_ordering(ev)
        seq = self._seq
        event_id = make_id("evt", seq)
        payload: dict[str, Any] = {
            "event_id": event_id,
            "prev_hash": self._last_hash,
            "batch_id": self.batch_id,
            "seq": seq,
            "wall_clock_at": wall_clock().isoformat(),
            **{k: _plain(v) for k, v in asdict(ev).items()},
        }
        payload["hash"] = sha256_of({k: v for k, v in payload.items() if k != "hash"})
        action = ev.action or {}
        self.conn.execute(
            "INSERT INTO events (event_id, batch_id, seq, prev_hash, hash, occurred_at,"
            " wall_clock_at, account_id, cycle_id, decision_id, stage, arm, action_type,"
            " action_hash, rule_failed, settled, amount_paise, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, self.batch_id, seq, payload["prev_hash"], payload["hash"],
                payload["occurred_at"], payload["wall_clock_at"], ev.account_id,
                ev.cycle_id, ev.decision_id, ev.stage.value,
                ev.arm.value if ev.arm else None,
                action.get("type"), ev.action_hash,
                (ev.policy or {}).get("check_failed"),
                _settled_flag(ev),
                action.get("amount_paise") or (ev.result or {}).get("amount_paise"),
                canonical_json(payload),
            ),
        )
        self.conn.commit()
        self._seq = seq + 1
        self._last_hash = payload["hash"]
        if ev.stage is Stage.ASSIGN and ev.account_id:
            self._assigned.add(ev.account_id)
        if ev.stage is Stage.CLOSE and ev.cycle_id:
            self._closed_cycles.add(ev.cycle_id)
        return Event(event_id, seq, payload["prev_hash"], payload["hash"], self.batch_id,
                     payload["wall_clock_at"], payload)

    def _check_ordering(self, ev: EventDraft) -> None:
        if ev.stage is Stage.ASSIGN:
            if not ev.account_id:
                raise LedgerError("ASSIGN requires an account_id")
            if ev.arm is None:
                raise LedgerError("ASSIGN must carry the arm")
            if ev.account_id in self._assigned:
                raise LedgerError(
                    f"arm already assigned for {ev.account_id}: assignment happens once, "
                    "first, and is immutable")
        elif ev.account_id and ev.stage is not Stage.DETECT and not ev.dry_run:
            if ev.account_id not in self._assigned:
                raise LedgerError(
                    f"{ev.stage.value} for {ev.account_id} before ASSIGN: the arm is "
                    "written before anything else touches the account")
        if ev.cycle_id and ev.cycle_id in self._closed_cycles:
            raise LedgerError(f"{ev.cycle_id} is closed; no events may follow a CLOSE")

    def record_batch(self, *, seed: int, config_json: str, config_hash: str,
                     policy_version: str, holdout_frac: float, lambda_harm: float,
                     status: str = "RUNNING", policy: str | None = None) -> None:
        """Stores the whole resolved config, not a path — so six weeks later you can say
        exactly what produced a number."""
        self.conn.execute(
            "INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, wall_clock().isoformat(), seed, config_json, config_hash,
             policy_version, holdout_frac, lambda_harm, status, policy),
        )
        self.conn.commit()

    def set_status(self, status: str) -> None:
        self.conn.execute("UPDATE batches SET status=? WHERE batch_id=?",
                          (status, self.batch_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- read queries --------------------------------------------------------

    def _rows(self, where: str, params: tuple[Any, ...]) -> list[Event]:
        sql = ("SELECT event_id, seq, prev_hash, hash, batch_id, wall_clock_at, payload"
               f" FROM events WHERE batch_id=? {where} ORDER BY seq")
        return [
            Event(r["event_id"], r["seq"], r["prev_hash"], r["hash"], r["batch_id"],
                  r["wall_clock_at"], json.loads(r["payload"]))
            for r in self.conn.execute(sql, (self.batch_id, *params))
        ]

    def all_events(self) -> list[Event]:
        return self._rows("", ())

    def timeline(self, account_id: str) -> list[Event]:
        return self._rows("AND account_id=?", (account_id,))

    def decision(self, decision_id: str) -> list[Event]:
        return self._rows("AND decision_id=?", (decision_id,))

    def denials(self) -> list[Event]:
        return self._rows("AND stage=? AND rule_failed IS NOT NULL", (Stage.GATE.value,))

    def denials_by_rule(self) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT rule_failed, COUNT(*) FROM events WHERE batch_id=? AND stage=?"
            " AND rule_failed IS NOT NULL GROUP BY rule_failed ORDER BY 2 DESC",
            (self.batch_id, Stage.GATE.value))]

    def recovery_by_arm(self) -> list[tuple[str, int, int]]:
        return [(r[0], r[1], r[2]) for r in self.conn.execute(
            "SELECT arm, COUNT(DISTINCT account_id),"
            " SUM(CASE WHEN settled=1 THEN amount_paise ELSE 0 END)"
            " FROM events WHERE batch_id=? AND stage=? GROUP BY arm",
            (self.batch_id, Stage.OBSERVE.value))]

    # ---- verification --------------------------------------------------------

    def verify(self, notice_hours: int = 24) -> VerifyReport:
        """Recompute the chain and check every invariant in docs/02-LEDGER.md."""
        rep = VerifyReport()
        rows = self.conn.execute(
            "SELECT seq, prev_hash, hash, payload FROM events WHERE batch_id=?"
            " ORDER BY seq", (self.batch_id,)).fetchall()
        rep.events = len(rows)
        prev = GENESIS
        events: list[dict[str, Any]] = []
        for i, r in enumerate(rows):
            payload = json.loads(r["payload"])
            events.append(payload)
            if r["seq"] != i:                                        # invariant 1
                rep.fail(f"seq gap: expected {i}, found {r['seq']}", r["seq"])
            if r["prev_hash"] != prev:                               # invariant 2
                rep.fail(f"broken chain at seq {r['seq']}", r["seq"])
            recomputed = sha256_of({k: v for k, v in payload.items() if k != "hash"})
            if recomputed != r["hash"] or payload.get("hash") != r["hash"]:
                rep.fail(f"hash mismatch at seq {r['seq']}: payload was modified",
                         r["seq"])
            prev = r["hash"]
        _check_account_order(events, rep)                            # invariant 3
        _check_execute_gated(events, rep)                            # invariant 4
        _check_holdout_untouched(events, rep)                        # invariant 5
        _check_close_terminal(events, rep)                           # invariants 6, 7
        _check_notice_precedes_retry(events, rep, notice_hours)      # invariant 8
        return rep


# ---- invariant checks: pure functions over the event list ----------------------

def _check_account_order(events: Iterable[dict[str, Any]], rep: VerifyReport) -> None:
    """First event for an account is DETECT, second is ASSIGN, ASSIGN occurs once."""
    seen: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        # Dry runs are evaluations, not actions: they may occur at any point, including
        # before the arm is assigned, because they neither read it nor change anything.
        if e.get("account_id") and not e.get("dry_run"):
            seen.setdefault(e["account_id"], []).append(e)
    for acc, evs in seen.items():
        stages = [e["stage"] for e in evs]
        if stages[0] != Stage.DETECT.value:
            rep.fail(f"{acc}: first event is {stages[0]}, expected DETECT", evs[0]["seq"])
        if len(stages) == 1:
            # Detected but not yet run: a simulated batch before `rr run` looks like this.
            # An arm that has not been assigned yet cannot have been read yet either.
            continue
        if stages[1] != Stage.ASSIGN.value:
            rep.fail(f"{acc}: second event is {stages[1]}, expected ASSIGN", evs[1]["seq"])
        n = stages.count(Stage.ASSIGN.value)
        if n != 1:
            rep.fail(f"{acc}: ASSIGN occurs {n} times, expected once", evs[0]["seq"])


def _check_execute_gated(events: Iterable[dict[str, Any]], rep: VerifyReport) -> None:
    """The machine-checkable form of 'nothing reached a customer without the gate'."""
    allowed: dict[tuple[Any, Any], int] = {}
    for e in events:
        if (e["stage"] == Stage.GATE.value
                and (e.get("policy") or {}).get("verdict") == Verdict.ALLOW.value):
            if e.get("dry_run"):
                # A hypothetical ALLOW authorises nothing. Without this, a verdict minted
                # by the demo endpoint would look to the verifier exactly like one that
                # had been through the runner.
                continue
            # Earliest wins. Events arrive in seq order, so the first ALLOW for a
            # decision is the one that authorised the execution — and keeping the last
            # instead meant any later gate evaluation of the same decision made an
            # already-executed action look like it had preceded its own approval.
            allowed.setdefault((e.get("decision_id"), e.get("action_hash")), e["seq"])
    for e in events:
        if e["stage"] != Stage.EXECUTE.value:
            continue
        if e.get("dry_run"):
            rep.fail(f"seq {e['seq']}: an EXECUTE is marked dry_run — a hypothetical "
                     "must never reach a rail", e["seq"])
            continue
        key = (e.get("decision_id"), e.get("action_hash"))
        if key not in allowed:
            rep.fail(f"seq {e['seq']}: EXECUTE with no matching ALLOW gate "
                     f"(decision={key[0]}, action_hash={key[1]})", e["seq"])
        elif allowed[key] > e["seq"]:
            rep.fail(f"seq {e['seq']}: EXECUTE precedes its own gate", e["seq"])


def _check_holdout_untouched(events: Iterable[dict[str, Any]],
                             rep: VerifyReport) -> None:
    for e in events:
        action_type = (e.get("action") or {}).get("type")
        if (e["stage"] == Stage.EXECUTE.value and e.get("arm") == Arm.HOLDOUT.value
                and action_type not in MERCHANT_DEFAULT_ACTIONS):
            rep.fail(f"seq {e['seq']}: holdout arm executed {action_type} — the control "
                     "group is contaminated", e["seq"])


def _check_close_terminal(events: Iterable[dict[str, Any]], rep: VerifyReport) -> None:
    closed: dict[str, int] = {}
    for e in events:
        cycle = e.get("cycle_id")
        if e["stage"] == Stage.CLOSE.value:
            if not (e.get("result") or {}).get("terminal_state"):
                rep.fail(f"seq {e['seq']}: CLOSE without a terminal_state", e["seq"])
            if cycle:
                closed[cycle] = e["seq"]
        elif cycle and cycle in closed:
            rep.fail(f"seq {e['seq']}: event for {cycle} after its CLOSE at seq "
                     f"{closed[cycle]}", e["seq"])
        result = e.get("result") or {}
        if result.get("ok") and not result.get("settled_at"):        # invariant 7
            rep.fail(f"seq {e['seq']}: ok result with no settled_at — recovery is "
                     "counted on settlement, not on an accepted request", e["seq"])


def _check_notice_precedes_retry(events: Iterable[dict[str, Any]], rep: VerifyReport,
                                 notice_hours: int) -> None:
    notices: dict[str, dict[str, Any]] = {}
    for e in events:
        action_type = (e.get("action") or {}).get("type")
        if e["stage"] != Stage.EXECUTE.value:
            continue
        result = e.get("result") or {}
        if action_type == ActionType.SEND_PREDEBIT_NOTICE.value:
            # Every receipt the notice issued, not just the first. One notice event can
            # carry several — a debit that will be presented in parts needs a receipt per
            # part — and indexing only the first made a split's reference look like a
            # reference to nothing.
            ids = result.get("notice_ids") or [result.get("notice_id") or e["event_id"]]
            for key in ids:
                notices[key] = e
        elif action_type in (ActionType.RETRY_DEBIT.value, ActionType.SPLIT_DEBIT.value):
            # One notice per presentation. A split that named a single receipt for three
            # parts would satisfy a check that only looked at `notice_ref`, which is the
            # whole coupling this invariant exists to enforce.
            refs = result.get("notice_ids") or (
                [e["notice_ref"]] if e.get("notice_ref") else [])
            expected = len(result.get("parts") or ()) or 1
            known = [r for r in refs if r in notices]
            if not known:
                rep.fail(f"seq {e['seq']}: debit with no referenced pre-debit notice",
                         e["seq"])
                continue
            if len(known) != expected:
                rep.fail(
                    f"seq {e['seq']}: debit presents {expected} part(s) against "
                    f"{len(known)} referenced pre-debit notice(s)", e["seq"])
                continue
            if len(set(known)) != len(known):
                rep.fail(f"seq {e['seq']}: the same notice authorised two presentations",
                         e["seq"])
                continue
            at = datetime.fromisoformat(e["occurred_at"])
            for ref in known:
                issued = datetime.fromisoformat(notices[ref]["occurred_at"])
                if at - issued < timedelta(hours=notice_hours):
                    rep.fail(
                        f"seq {e['seq']}: debit {at - issued} after its notice, inside "
                        f"the {notice_hours}h window", e["seq"])
                    break


# ---- helpers ------------------------------------------------------------------

def _plain(v: Any) -> Any:
    """Enums to their string values, tuples to lists, datetimes to ISO — so the payload
    round-trips through JSON identically on every run."""
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _settled_flag(ev: EventDraft) -> int | None:
    if not ev.result or "ok" not in ev.result:
        return None
    return 1 if ev.result.get("ok") and ev.result.get("settled_at") else 0
