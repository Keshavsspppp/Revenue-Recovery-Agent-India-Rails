"""Batch generation: build a `World`, persist it, and write the DETECT events.

Latent state goes in a separate `latent_truth` table used only by the simulator and by
the evaluator's hardship-detector scoring. `tests/test_boundaries.py` asserts that no
module under app/plan, app/diagnose or app/propose so much as names it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from app.domain.clock import IST
from app.domain.codemap import load_codemap
from app.domain.config import Config
from app.domain.enums import Stage
from app.domain.models import canonical_json, make_id
from app.domain.money import amount_band
from app.ledger import EventDraft, Ledger
from app.sim.world import ATTEMPT_TIME, World

SIM_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    merchant_category TEXT NOT NULL,
    city_tier         INTEGER NOT NULL,
    consent_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mandates (
    mandate_id    TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    rail          TEXT NOT NULL,
    cap_paise     INTEGER NOT NULL,
    status        TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    defect        TEXT
);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id     TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    due_date     TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    first_failure_code TEXT NOT NULL
);

-- Latent truth. The agent must never read this; the evaluator may, to score the
-- hardship detector and the cause posterior against ground truth.
CREATE TABLE IF NOT EXISTS latent_truth (
    account_id     TEXT PRIMARY KEY,
    inflow_day     INTEGER NOT NULL,
    inflow_paise   INTEGER NOT NULL,
    balance_paise  INTEGER NOT NULL,
    burn_rate      REAL NOT NULL,
    intent         REAL NOT NULL,
    hardship       INTEGER NOT NULL,
    dispute_prone  INTEGER NOT NULL,
    responsiveness REAL NOT NULL,
    mandate_defect TEXT,
    true_cause     TEXT
);

CREATE INDEX IF NOT EXISTS idx_mandates_account ON mandates(account_id);
CREATE INDEX IF NOT EXISTS idx_cycles_account   ON cycles(account_id);
"""


def simulate_batch(cfg: Config, *, seed: int, n_accounts: int, out_path: str | Path,
                   batch_id: str | None = None, start: date | None = None) -> tuple[World, str]:
    """Generate a batch and write it to `out_path`. Returns the world and its batch_id."""
    world = World.generate(cfg, seed=seed, n_accounts=n_accounts, start=start,
                           codemap=load_codemap())
    batch_id = batch_id or make_id("bat", seed)

    led = Ledger(out_path, batch_id)
    led.conn.executescript(SIM_DDL)
    led.record_batch(seed=seed, config_json=canonical_json(cfg.raw),
                     config_hash=cfg.world_hash, policy_version=cfg.policy_version,
                     holdout_frac=cfg.holdout_frac, lambda_harm=cfg.lambda_harm,
                     status="READY")

    for account_id, account in world.accounts.items():
        led.conn.execute(
            "INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?)",
            (account_id, account.merchant_category.value, account.city_tier,
             json.dumps({"channels_allowed": sorted(c.value for c in
                                                    account.consent.channels_allowed),
                         "dnd_registered": account.consent.dnd_registered,
                         "recording_consent": account.consent.recording_consent,
                         "purpose": account.consent.purpose}),
             account.created_at.isoformat()))

        for m in world.mandates[account_id]:
            led.conn.execute(
                "INSERT OR REPLACE INTO mandates VALUES (?,?,?,?,?,?,?)",
                (m.mandate_id, m.account_id, m.rail.value, m.cap_paise, m.status.value,
                 m.registered_at.isoformat(), m.defect.value if m.defect else None))

        state = world.cycles[account_id]
        c = state.cycle
        led.conn.execute(
            "INSERT OR REPLACE INTO cycles VALUES (?,?,?,?,?,?)",
            (c.cycle_id, c.account_id, c.amount_paise, c.due_date.isoformat(),
             c.horizon_days, state.first_failure_code))

        lat = world.latent[account_id]
        led.conn.execute(
            "INSERT OR REPLACE INTO latent_truth VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, lat.inflow_day, lat.inflow_paise, lat.balance_paise,
             lat.burn_rate, lat.intent, int(lat.hardship), int(lat.dispute_prone),
             lat.responsiveness,
             lat.mandate_defect.value if lat.mandate_defect else None,
             lat.true_cause.value if lat.true_cause else None))

    led.conn.commit()

    # One DETECT per account: revenue at risk, observed. The arm is assigned by the
    # runner (M3) before anything else touches the account — that ordering is enforced
    # by the ledger itself.
    detected_at = datetime.combine(world.start, ATTEMPT_TIME, tzinfo=IST)
    for account_id, state in world.cycles.items():
        led.append(EventDraft(
            stage=Stage.DETECT, occurred_at=detected_at, account_id=account_id,
            cycle_id=state.cycle.cycle_id,
            result={"ok": False, "rail_code": state.first_failure_code,
                    "settled_at": None, "amount_paise": state.cycle.amount_paise},
            evidence=(f"rail_code:{state.first_failure_code}",
                      f"amount_band:{amount_band(state.cycle.amount_paise)}",
                      f"city_tier:{world.accounts[account_id].city_tier}",
                      "attempts_made:1")))
    led.close()
    return world, batch_id
