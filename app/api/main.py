"""The HTTP surface. docs/09-API.md.

No business logic lives here — every endpoint is a thin wrapper over a function the CLI
already calls, so the two surfaces cannot disagree about what a number means. If a figure
differs between `rr report` and `GET /scoreboard`, that is a bug in this file, not a
difference of opinion.

No auth. It is a local demo, and saying so is better than half-building one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.domain.config import Config
from app.domain.enums import ActionType, Channel, Rail, Stage
from app.domain.models import Action

# The API is an entry point, so it reads `.env` here exactly as `app.cli.main` does.
# The rule this does not break is the one about *constructors*: `RazorpayTestAdapter
# .from_env` and `GroqProposer.from_env` still never repopulate the environment, because
# a constructor that does can put a live credential back under a test that removed it.
# Without this line `POST /live/sync` could never reach Razorpay from `uvicorn`.
from app.domain.env import load_dotenv

load_dotenv()

DATA = Path(__file__).resolve().parents[2] / "data"

app = FastAPI(title="Revenue Recovery — India Rails",
              description="Detect, diagnose, act under a compliance gate, and prove the "
                          "lift was real. docs/09-API.md",
              version="0.1.0")


def _cfg() -> Config:
    return Config.load()


def _batch_path(batch: str) -> str:
    """Batch ids and file stems are both accepted, because a demo types whichever is to
    hand. Resolved paths must stay under `data/` — the app has no auth by design, so the
    one thing it must not do is open an arbitrary file off the disk because a path
    arrived in the URL. `data/ablation/ablate_groq` still works; `../../secrets` does
    not.
    """
    root = DATA.resolve()
    for candidate in (DATA / batch, DATA / f"{batch}.db"):
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            continue
        if resolved.is_file() and resolved.suffix == ".db":
            return str(resolved)
    raise HTTPException(404, f"no such batch: {batch}")


# ---- batches -------------------------------------------------------------------

@app.get("/batches", summary="List the batches on disk")
def list_batches() -> list[dict[str, Any]]:
    out = []
    for path in sorted(DATA.glob("*.db"), key=lambda p: -p.stat().st_mtime):
        try:
            con = sqlite3.connect(str(path))
            row = con.execute(
                "SELECT batch_id, seed, policy, status, holdout_frac, lambda_harm"
                " FROM batches LIMIT 1").fetchone()
            accounts = con.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
            events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            con.close()
        except sqlite3.Error:
            continue
        if row:
            out.append({"file": path.stem, "mtime": path.stat().st_mtime,
                        "batch_id": row[0], "seed": row[1],
                        "policy": row[2] or "not run", "status": row[3],
                        "holdout_frac": row[4], "lambda_harm": row[5],
                        "accounts": accounts, "events": events})
    return out


@app.get("/batches/{batch}/scoreboard", summary="The full report as JSON")
def scoreboard(batch: str, bootstrap: int = Query(4000, ge=0, le=20000)) -> Any:
    from app.eval.report import as_json, build

    path = _batch_path(batch)
    con = sqlite3.connect(path)
    ran = con.execute("SELECT policy FROM batches LIMIT 1").fetchone()
    con.close()
    if not ran or not ran[0]:
        # A simulated-but-unrun batch has no arms and no outcomes, so every figure on the
        # scoreboard would be a division by zero. Saying so beats a 500.
        raise HTTPException(409, f"batch '{batch}' has been simulated but not run. "
                                 f"Run `rr run --batch data/{batch}.db --policy agent`.")
    board, meta = build(path, bootstrap_n=bootstrap)
    return json.loads(as_json(board, meta))


@app.get("/batches/{batch}/verify", summary="Hash chain and every ledger invariant")
def verify(batch: str) -> dict[str, Any]:
    from app.ledger import Ledger

    path = _batch_path(batch)
    con = sqlite3.connect(path)
    batch_id = con.execute("SELECT batch_id FROM batches LIMIT 1").fetchone()[0]
    con.close()
    led = Ledger(path, batch_id)
    report = led.verify()
    led.close()
    return {"ok": report.ok, "events": report.events,
            "first_bad_seq": report.first_bad_seq, "failures": report.failures[:20]}


@app.get("/batches/{batch}/denials", summary="Every denied action, grouped by rule")
def denials(batch: str, rule_id: str | None = None) -> dict[str, Any]:
    path = _batch_path(batch)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    by_rule = dict(con.execute(
        "SELECT rule_failed, COUNT(*) FROM events WHERE stage=? AND rule_failed IS NOT NULL"
        " GROUP BY rule_failed ORDER BY 2 DESC", (Stage.GATE.value,)).fetchall())
    where = " AND rule_failed=?" if rule_id else ""
    params: tuple = (Stage.GATE.value, rule_id) if rule_id else (Stage.GATE.value,)
    examples = [
        {"account_id": r["account_id"], "occurred_at": r["occurred_at"],
         "action": r["action_type"], "rule": r["rule_failed"],
         "reason": (json.loads(r["payload"]).get("policy") or {}).get("reason"),
         "basis": (json.loads(r["payload"]).get("policy") or {}).get("basis")}
        for r in con.execute(
            "SELECT account_id, occurred_at, action_type, rule_failed, payload FROM events"
            f" WHERE stage=? AND rule_failed IS NOT NULL{where} ORDER BY seq LIMIT 40",
            params)]
    con.close()
    return {"by_rule": by_rule, "examples": examples}


# ---- the audit trail -------------------------------------------------------------

@app.get("/batches/{batch}/accounts/{account_id}/timeline",
         summary="One account, end to end")
def timeline(batch: str, account_id: str) -> list[dict[str, Any]]:
    from app.ledger import Ledger

    path = _batch_path(batch)
    con = sqlite3.connect(path)
    batch_id = con.execute("SELECT batch_id FROM batches LIMIT 1").fetchone()[0]
    con.close()
    led = Ledger(path, batch_id)
    events = led.timeline(account_id)
    led.close()
    if not events:
        raise HTTPException(404, f"no events for {account_id}")
    return [{"seq": e.seq, "occurred_at": e.payload["occurred_at"],
             "stage": e.payload["stage"],
             "action": (e.payload.get("action") or {}).get("type"),
             "arm": e.payload.get("arm"),
             "decision_id": e.payload.get("decision_id"),
             "rule_failed": (e.payload.get("policy") or {}).get("check_failed"),
             "reason": (e.payload.get("policy") or {}).get("reason"),
             "basis": (e.payload.get("policy") or {}).get("basis"),
             "posterior": e.payload.get("cause_posterior"),
             "evidence": e.payload.get("evidence"),
             "result": e.payload.get("result"),
             "notes": e.payload.get("notes"),
             "hash": e.hash} for e in events]


@app.get("/batches/{batch}/interesting", summary="Accounts worth clicking in a demo")
def interesting(batch: str) -> dict[str, list[str]]:
    """The demo needs a *deliberately interesting* account, not the first one.

    docs/11-DEMO.md wants an AP17 NRE mandate repaired onto another rail. This finds the
    accounts that actually did something worth watching, so nobody has to hunt live.
    """
    path = _batch_path(batch)
    con = sqlite3.connect(path)
    picks: dict[str, list[str]] = {}
    for label, sql, params in (
        ("mandate_repaired",
         "SELECT DISTINCT account_id FROM events WHERE stage=? AND action_type=? LIMIT 5",
         (Stage.EXECUTE.value, ActionType.REREGISTER_MANDATE.value)),
        ("stopped_early",
         "SELECT DISTINCT account_id FROM events WHERE stage=? AND json_extract("
         "payload,'$.result.terminal_state')='EV_BELOW_THRESHOLD' LIMIT 5",
         (Stage.CLOSE.value,)),
        ("hardship_exit",
         "SELECT DISTINCT account_id FROM events WHERE stage=? AND json_extract("
         "payload,'$.result.terminal_state')='HARDSHIP' LIMIT 5",
         (Stage.CLOSE.value,)),
        ("recovered",
         "SELECT DISTINCT account_id FROM events WHERE stage=? AND settled=1 LIMIT 5",
         (Stage.OBSERVE.value,)),
        ("denied",
         "SELECT DISTINCT account_id FROM events WHERE stage=? AND rule_failed"
         " IS NOT NULL LIMIT 5", (Stage.GATE.value,)),
        ("live_razorpay",
         "SELECT DISTINCT account_id FROM events WHERE stage=? AND json_extract("
         "payload,'$.result.provider')='razorpay_test' LIMIT 5", (Stage.EXECUTE.value,)),
    ):
        picks[label] = [r[0] for r in con.execute(sql, params)]
    con.close()
    return picks


# ---- the gate --------------------------------------------------------------------

class EvaluateRequest(BaseModel):
    batch: str
    account_id: str
    action: str = Field(default="VOICE_CONFIRM_PTP")
    channel: str | None = "VOICE"
    rail: str | None = None
    template_id: str | None = "DLT_RECOVERY_PTP_001"
    at: str = Field(description="Simulated IST time, ISO-8601")


@app.post("/policy/evaluate", summary="Evaluate an action against the real gate")
def policy_evaluate(request: EvaluateRequest) -> dict[str, Any]:
    """Executes nothing. This is the endpoint the demo clicks: set 19:30, submit a voice
    call, watch the gate refuse it — and the refusal is written to the ledger, so the
    denial produced on stage appears in the trail shown thirty seconds later."""
    from app.policy.evaluate_api import UnknownAccount, evaluate_hypothetical

    cfg = _cfg()
    try:
        action = Action(
            type=ActionType[request.action],
            channel=Channel[request.channel] if request.channel else None,
            rail=Rail[request.rail] if request.rail else None,
            template_id=request.template_id,
            cli_series=cfg.raw["policy"]["cli_series_service"],
            disclosure=True)
    except KeyError as exc:
        raise HTTPException(400, f"unknown enum value: {exc}") from None
    try:
        return evaluate_hypothetical(_batch_path(request.batch), request.account_id,
                                     action, datetime.fromisoformat(request.at), cfg)
    except UnknownAccount as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, f"bad timestamp: {exc}") from None


@app.get("/policy/rules", summary="The rule catalogue, with the regulation behind each")
def policy_rules() -> dict[str, Any]:
    from app.policy import catalogue, rules_hash

    return {"version": _cfg().policy_version, "rules_hash": rules_hash(),
            "rules": catalogue()}


# ---- diagnostics -----------------------------------------------------------------

@app.get("/batches/{batch}/diagnostics", summary="Scored against latent truth")
def diagnostics(batch: str) -> dict[str, Any]:
    from app.eval.diagnostics import action_hit_rate, cause_accuracy, hardship_detector

    path = _batch_path(batch)
    return {"hardship_detector": hardship_detector(path).as_dict(),
            "cause_accuracy": cause_accuracy(path),
            "action_hit_rate": action_hit_rate(path)}


@app.get("/live/references", summary="Real Razorpay objects this batch created")
def live_references(batch: str) -> list[dict[str, Any]]:
    path = _batch_path(batch)
    con = sqlite3.connect(path)
    rows = [{"account_id": a, **json.loads(p)["result"]} for a, p in con.execute(
        "SELECT account_id, payload FROM events WHERE stage=? AND json_extract("
        "payload,'$.result.provider')='razorpay_test' ORDER BY seq",
        (Stage.EXECUTE.value,))]
    con.close()
    return rows


class SyncRequest(BaseModel):
    batch: str


@app.post("/live/sync", summary="Ask Razorpay what became of the objects we created")
def live_sync(request: SyncRequest) -> list[dict[str, Any]]:
    """Read-back, not a re-send. Nothing new is created.

    A link that has been paid is written to the ledger as a confirmed recovery, once —
    recovery is counted from what the provider confirms, never from the fact that we
    asked. A link nobody has paid produces no payment, which is why the dashboard's
    Payments screen stays empty while Payment Links fills up.
    """
    from app.rails import RazorpayTestAdapter, RazorpayUnavailable
    from app.sync import sync_live

    try:
        adapter = RazorpayTestAdapter.from_env(_cfg())
    except RazorpayUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    links = sync_live(_batch_path(request.batch), _cfg(), adapter)
    return [{"account_id": x.account_id, "provider_id": x.provider_id,
             "status": x.status, "amount_paise": x.amount_paise,
             "amount_paid_paise": x.amount_paid_paise, "settled": x.settled,
             "paid_at": x.paid_at.isoformat() if x.paid_at else None,
             "url": x.url} for x in links]


# ---- the page --------------------------------------------------------------------

# The built React app, when `npm run build` has been run in `web/`. Mounted rather than
# required: the JSON API is the contract and it works with or without a dashboard.
WEB = Path(__file__).resolve().parent / "web"
if (WEB / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    built = WEB / "index.html"
    if built.exists():
        return built.read_text(encoding="utf-8")
    # An error names the fix. The build output is gitignored, so a fresh clone lands here.
    return ("<h1>The dashboard has not been built</h1>"
            "<p>Run <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>, "
            "then reload. The JSON API is live either way — see "
            "<a href='/docs'>/docs</a>.</p>")
