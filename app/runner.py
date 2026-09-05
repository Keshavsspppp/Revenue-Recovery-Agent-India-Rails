"""The run loop: assign arms, drive the world day by day, write every stage to the ledger.

The world is *regenerated* from the batch's stored seed and config rather than reloaded
from tables. Same seed plus same config gives the same world, so the database only has to
carry the ledger and the record of what was generated. The config hash is checked before
anything runs: a batch simulated under one config and run under another would silently
compare two different worlds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.diagnose import explain_hardship
from app.domain.clock import IST, Clock
from app.domain.codemap import load_codemap
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    Arm,
    Channel,
    Stage,
    TerminalState,
    Verdict,
)
from app.domain.models import Action, Budgets, GateDecision, Mandate, make_id
from app.eval.arms import Stratum, assign_arms, stratum_of
from app.ledger import EventDraft, Ledger
from app.policies import IMPLEMENTED_NAMES, merchant_default
from app.policies import build as build_policy
from app.policy import AccountFlags, Calendar, GateContext, PolicySet, evaluate
from app.sim.world import ATTEMPT_TIME, NOTICE_TIME, World

#: Policies that exist so far. `agent` is M8.
IMPLEMENTED_POLICIES = IMPLEMENTED_NAMES


class ConfigDrift(Exception):
    """The batch was simulated under a different config than it is being run under."""


class AlreadyRun(Exception):
    """This batch already has a run recorded. The ledger is append-only."""


@dataclass(frozen=True)
class BatchMeta:
    batch_id: str
    seed: int
    n_accounts: int
    config_hash: str
    holdout_frac: float


def read_batch_meta(path: str | Path) -> BatchMeta:
    con = sqlite3.connect(str(path))
    row = con.execute("SELECT batch_id, seed, config_hash, holdout_frac FROM batches"
                      " LIMIT 1").fetchone()
    n = con.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    con.close()
    if row is None:
        raise ConfigDrift(f"{path} has no batch record — run `rr simulate` first")
    return BatchMeta(batch_id=row[0], seed=row[1], n_accounts=n, config_hash=row[2],
                     holdout_frac=row[3])


def run_batch(path: str | Path, cfg: Config, policy: str = "nothing",
              holdout_frac: float | None = None, dry_run: bool = False,
              proposer=None) -> BatchMeta:
    """Run `policy` over an already-simulated batch, writing the full trail."""
    # A callable is accepted as well as a name, so a test can drive one action through
    # the real executor without hand-building it. Named policies are still the only
    # thing the CLI will run.
    if not callable(policy) and policy not in IMPLEMENTED_POLICIES:
        raise NotImplementedError(
            f"policy '{policy}' is not built yet — see docs/10-BUILD-PLAN.md "
            f"(implemented: {', '.join(IMPLEMENTED_POLICIES)})")

    meta = read_batch_meta(path)
    if meta.config_hash != cfg.world_hash:
        raise ConfigDrift(
            "config has changed since this batch was simulated. The world is regenerated "
            "from the seed, so running it under a different config would compare two "
            f"different worlds.\n  simulated under {meta.config_hash}\n  running under "
            f"{cfg.config_hash}\nRe-run `rr simulate`, or restore the original config.")

    holdout_frac = cfg.holdout_frac if holdout_frac is None else holdout_frac
    world = World.generate(cfg, seed=meta.seed, n_accounts=meta.n_accounts,
                           codemap=load_codemap())
    led = Ledger(path, meta.batch_id)

    # A batch that has already executed anything cannot be run again.
    #
    # Not because the ledger would reject it (it would — closed cycles), but for a
    # subtler reason worth stating: the world is regenerated from the seed, while the
    # executor's idempotency keys are rebuilt from the ledger. Resuming would therefore
    # suppress actions the fresh world has not performed — a suppressed notice would
    # leave the following retry failing its notice window — so the world and the ledger
    # would silently disagree. Correct resume needs the world's effects replayed, which
    # is not built. Refusing is honest; pretending to resume would not be.
    already = led.conn.execute(
        "SELECT COUNT(*) FROM events WHERE batch_id=? AND stage IN (?,?)",
        (meta.batch_id, Stage.EXECUTE.value, Stage.CLOSE.value)).fetchone()[0]
    if already:
        led.close()
        raise AlreadyRun(
            f"batch {meta.batch_id} has already been run ({already} execute/close "
            "events). The ledger is append-only and the world is regenerated from the "
            "seed, so a second run cannot be reconciled with the first. Re-run "
            "`rr simulate` to a fresh path to run a different policy.")

    # ---- 1. Arms, before anything else touches an account --------------------
    strata: dict[str, Stratum] = {
        account_id: stratum_of(
            world.codemap.cause_of(state.first_failure_code),
            state.cycle.amount_paise,
            world.accounts[account_id].merchant_category)
        for account_id, state in world.cycles.items()
    }
    # An arm that has already been written is read back, never recomputed. Re-deriving it
    # would mean a config or code change could silently move accounts between arms in the
    # middle of a batch, which is precisely how holdouts get corrupted. Assignment happens
    # once, first, and is immutable — the ledger enforces it, and this respects it, which
    # is what makes a re-run a resume rather than an error.
    existing = {r[0]: Arm(r[1]) for r in led.conn.execute(
        "SELECT account_id, arm FROM events WHERE batch_id=? AND stage=?",
        (meta.batch_id, Stage.ASSIGN.value))}
    if existing:
        missing = set(world.cycles) - set(existing)
        if missing:
            raise ConfigDrift(
                f"{len(missing)} accounts have no ASSIGN event but others do. This batch "
                "was interrupted mid-assignment; the arms cannot be trusted. Re-simulate.")
        arms = existing
    else:
        arms = assign_arms(strata, holdout_frac, meta.seed)
        assigned_at = datetime.combine(world.start, ATTEMPT_TIME, tzinfo=IST)
        for account_id in sorted(world.cycles):
            stratum = strata[account_id]
            led.append(EventDraft(
                stage=Stage.ASSIGN, occurred_at=assigned_at, account_id=account_id,
                cycle_id=world.cycles[account_id].cycle.cycle_id, arm=arms[account_id],
                evidence=(f"stratum_cause:{stratum.cause.value}",
                          f"stratum_band:{stratum.band}",
                          f"stratum_category:{stratum.category.value}",
                          f"holdout_frac:{holdout_frac}"),
                notes=f"stratified assignment, seed {meta.seed}"))

    # ---- 2. The day loop ------------------------------------------------------
    executor = _Executor(world, led, arms, cfg, dry_run=dry_run, policy_name=getattr(policy, "__name__", policy) if callable(policy) else policy,
                         proposer=proposer)
    treatment_policy = policy if callable(policy) else build_policy(policy, cfg)
    seen: set[tuple[str, str]] = set()
    clock = Clock(datetime.combine(world.start, time(0), tzinfo=IST))

    for offset in range(cfg.horizon_days + 1):
        day = world.start + timedelta(days=offset)
        clock.advance_to(day, time(0))
        world.tick_day(day)

        _resolve_promises(world, led, arms, executor, day, cfg)

        for account_id in world.open_accounts():
            # The holdout arm receives the merchant default and nothing else, whatever
            # the treatment arm is running. This one line is the experiment.
            acting = (merchant_default if arms[account_id] is Arm.HOLDOUT
                      else treatment_policy)
            acting(executor, world, account_id, day, offset)

        # Self-pay runs for every unsettled cycle, including ones the agent has closed.
        # Stopping the workflow is the agent declining to spend, not the customer
        # declining to pay.
        for account_id in world.unsettled_accounts():
            world.roll_self_pay(account_id, day)

        seen = _observe_settlements(world, led, arms, day, seen)

    # ---- 3. Close every cycle with exactly one terminal state -----------------
    closed_at = datetime.combine(world.start + timedelta(days=cfg.horizon_days),
                                 time(23, 0), tzinfo=IST)
    for account_id in sorted(world.cycles):
        state = world.cycles[account_id]
        # A promise still open when the horizon runs out lapses rather than breaking: the
        # customer never reached the date they named.
        lapsed = world.lapse_ptp(account_id)
        if lapsed is not None:
            led.append(EventDraft(
                stage=Stage.OBSERVE, occurred_at=closed_at, account_id=account_id,
                cycle_id=state.cycle.cycle_id, arm=arms[account_id],
                result={"ptp_status": lapsed.value, "ptp_id": state.ptp.ptp_id,
                        "paid_paise": state.paid_paise},
                evidence=(f"ptp_status:{lapsed.value}",
                          f"promised_date:{state.ptp.promised_date.isoformat()}")))
        terminal = state.terminal or TerminalState.CYCLE_ENDED
        # One settlement observation per cycle. Only this event carries `ok`, so the
        # scoreboard's SUM over settled rows can never double-count an account.
        led.append(EventDraft(
            stage=Stage.OBSERVE, occurred_at=closed_at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, arm=arms[account_id],
            result={"ok": state.settled_at is not None,
                    "rail_code": "SUCCESS" if state.settled_at else "",
                    "settled_at": state.settled_at.isoformat() if state.settled_at else None,
                    "settled_source": state.settled_source,
                    "amount_paise": state.cycle.amount_paise,
                    "opted_out": state.opted_out,
                    "complained": state.complained,
                    "disputed": state.disputed,
                    "mandate_cancelled": state.mandate_cancelled,
                    "contacts": len(state.contacts),
                    "hardship_flagged": state.terminal is TerminalState.HARDSHIP,
                    "attempts": len(state.attempts) - 1},   # the first failure is given
            evidence=(f"first_failure_code:{state.first_failure_code}",
                      f"settled_source:{state.settled_source or 'none'}")))
        led.append(EventDraft(
            stage=Stage.CLOSE, occurred_at=closed_at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, arm=arms[account_id],
            result={"terminal_state": terminal.value}))

    led.set_status("COMPLETE")
    led.conn.execute("UPDATE batches SET holdout_frac=?, policy=? WHERE batch_id=?",
                     (holdout_frac,
                      getattr(policy, "__name__", policy) if callable(policy) else policy,
                      meta.batch_id))
    led.conn.commit()
    led.close()
    return meta


class _Executor:
    """Gate, execute, record. Every rail operation goes through here so that no code path
    can reach an adapter without leaving a GATE event behind it — whether it passed or
    failed."""

    #: Times of day the schedule uses. Notices go out the morning before a debit is due.
    notice_time = NOTICE_TIME
    attempt_time = ATTEMPT_TIME

    def __init__(self, world: World, led: Ledger, arms: dict[str, Arm], cfg: Config,
                 dry_run: bool = False, policy_name: str = "nothing",
                 proposer=None) -> None:
        self.policy_name = policy_name
        self.proposer = proposer
        self.world = world
        self.led = led
        self.arms = arms
        self.cfg = cfg
        self.dry_run = dry_run
        self._decision_seq = 0
        self.duplicates_suppressed = 0
        self.policy = PolicySet.from_config(cfg)
        self.calendar = Calendar.from_config(cfg)
        self.codemap = load_codemap()
        # Harm is priced against the batch median so lambda means the same thing across a
        # heavy-tailed batch. Computed once, from observable cycle amounts.
        amounts = sorted(s.cycle.amount_paise for s in world.cycles.values())
        self.amount_scale = amounts[len(amounts) // 2] if amounts else 0
        self._decision_for: dict[str, str] = {}
        # Idempotency keys are rebuilt from the ledger rather than kept only in memory,
        # so a re-run over a partially-written batch is idempotent too. In-process
        # de-duplication alone would let a crash-restart charge an account twice.
        self._done: set[tuple[str, str, str, str]] = {
            (r[0], r[1], r[2], r[3][:10]) for r in led.conn.execute(
                "SELECT account_id, cycle_id, action_type, occurred_at FROM events"
                " WHERE batch_id=? AND stage=?", (led.batch_id, Stage.EXECUTE.value))}

    def _claim(self, account_id: str, action_type: ActionType, at: datetime) -> bool:
        """One action of a given type per account, per cycle, per day. Returns False if
        this exact action has already been executed — the caller must then do nothing at
        all: no gate, no rail call, no event."""
        key = (account_id, self.world.cycles[account_id].cycle.cycle_id,
               action_type.value, at.date().isoformat())
        if key in self._done:
            self.duplicates_suppressed += 1
            return False
        self._done.add(key)
        return True

    def context(self, account_id: str, at: datetime) -> GateContext:
        """Assemble what the rules may look at. Everything here is observable state —
        nothing from the simulator's latent truth."""
        world, state = self.world, self.world.cycles[account_id]
        spent = state.fees_paise
        return GateContext(
            now=at,
            account=world.accounts[account_id],
            consent=world.accounts[account_id].consent,
            mandates=tuple(world.mandates.get(account_id, ())),
            cycle=state.cycle,
            budgets=Budgets(
                attempts_remaining=max(
                    0, self.cfg.budgets.attempts_per_cycle - (len(state.attempts) - 1)),
                contacts_remaining_week=max(
                    0, self.cfg.budgets.contacts_per_week
                    - len(state.recovery_contacts)),
                # Was the constant budget, so POL-FREQ-003 could never deny and the
                # cap on the most intrusive action in the set was decorative.
                voice_remaining_cycle=max(
                    0, self.cfg.budgets.voice_per_cycle - state.voice_calls),
                spend_remaining_paise=max(
                    0, self.cfg.budgets.spend_per_cycle_paise - spent)),
            contacts_made=tuple(state.recovery_contacts),
            # This account's notices only. Handing the gate every notice in the batch
            # was both O(batch) per evaluation and wrong in principle: one account's
            # compliance context has no business containing another's.
            # Live copies. The cycle's own list is what it was handed at issue time and
            # never learns that a receipt was spent, so reading it directly makes
            # POL-NOTICE-003 unable to fire in a run.
            notices=tuple(self.world.rails.current(r) for r in state.notices),
            ptp=state.ptp,
            flags=AccountFlags(
                disputed=state.disputed,
                terminal_state=state.terminal.value if state.terminal else None),
            calendar=self.calendar,
            cfg=self.cfg)

    def open_decision(self, account_id: str, at: datetime, posterior: dict,
                      eligible, choice, inflow, proposal=None, hardship=None,
                      consulted: bool = False) -> str:
        """Write DIAGNOSE, ELIGIBLE and PROPOSE under one decision_id.

        That grouping is what makes the trail *answerable* rather than merely complete:
        one id spans the diagnosis, the actions it permitted, the price of each, and the
        one that was taken.
        """
        from app.diagnose import explain

        self._decision_seq += 1
        decision_id = make_id("dec", self._decision_seq)
        self._decision_for[account_id] = decision_id
        state = self.world.cycles[account_id]
        common = dict(occurred_at=at, account_id=account_id,
                      cycle_id=state.cycle.cycle_id, decision_id=decision_id,
                      arm=self.arms[account_id])
        self.led.append(EventDraft(
            stage=Stage.DIAGNOSE, cause_posterior={c.value: round(p, 4)
                                                   for c, p in posterior.items() if p > 0.001},
            evidence=(f"rail_code:{state.attempts[-1][1] if state.attempts else ''}",
                      f"attempts_made:{max(0, len(state.attempts) - 1)}",
                      f"inflow_day_est:{inflow.day_of_month}",
                      f"inflow_confidence:{inflow.concentration:.2f}",
                      f"inflow_observations:{inflow.n_observations}",
                      *(explain_hardship(hardship[1], hardship[0]) if hardship else ())),
            model={"cause": "cause_bayes_0.1.0"}, **common))
        self.led.append(EventDraft(
            stage=Stage.ELIGIBLE, evidence=tuple(explain(eligible)), **common))
        self.led.append(EventDraft(
            stage=Stage.PROPOSE,
            action={"type": choice.action.value},
            model={"planner": "budget_vi_0.1.0",
                   "proposer": proposal.source if proposal else (
                       self.proposer.name if self.proposer else "planner-argmax"),
                   "latency_ms": proposal.latency_ms if proposal else 0,
                   "cached": bool(proposal and proposal.cached),
                   # `consulted` is not a detail. Without it a decision the proposer was
                   # never asked about is indistinguishable from one it answered badly,
                   # so both the fallback count and the agreement rate are computed over
                   # the wrong denominator — the agreement rate in particular becomes a
                   # statement about the planner agreeing with itself.
                   "consulted": consulted,
                   "fell_back": consulted and proposal is None},
            evidence=choice.evidence() + (proposal.evidence() if proposal else ()),
            notes=choice.reason, **common))
        return decision_id

    def unexecutable(self, account_id: str, at: datetime, action_type: ActionType,
                     why: str) -> None:
        """The planner chose something the executor cannot carry out.

        Written to the ledger rather than dropped. Silently doing nothing is how the
        value function ends up pricing an action that is really a free WAIT, and the
        trail shows a decision with no consequence and no explanation for it.
        """
        state = self.world.cycles[account_id]
        self.led.append(EventDraft(
            stage=Stage.OBSERVE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id,
            decision_id=self._decision_for.get(account_id),
            arm=self.arms[account_id], action={"type": action_type.value},
            result={"outcome": "NOT_EXECUTED", "reason": why},
            evidence=(f"not_executed:{action_type.value}", f"reason:{why}"),
            notes="chosen by the planner, no execution path taken"))

    def close(self, account_id: str, at: datetime, terminal, reason: str) -> None:
        """End the cycle. The terminal state is a result of the arithmetic, not a
        constant — nobody typed 'try four times' anywhere in this system."""
        state = self.world.cycles[account_id]
        if state.terminal is not None:
            return
        state.terminal = terminal
        self.led.append(EventDraft(
            stage=Stage.OBSERVE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id,
            decision_id=self._decision_for.get(account_id),
            arm=self.arms[account_id],
            result={"terminal_state": terminal.value}, notes=reason))

    def reregister(self, account_id: str, rail, at: datetime) -> None:
        """Repair the mandate onto another rail. The customer still has to complete the
        authorisation, so this buys a chance, not a mandate."""
        state = self.world.cycles[account_id]
        action = Action(type=ActionType.REREGISTER_MANDATE, target_rail=rail)
        decision_id, gate = self._gate(action, at, account_id)
        if gate.verdict is not Verdict.ALLOW or self.dry_run:
            return
        cap = max(state.cycle.amount_paise * 3, 1_500_000)
        mandate = self.world.rails.register_mandate(account_id, rail, cap, at, gate)
        cost = self.cfg.action_cost_paise(ActionType.REREGISTER_MANDATE)
        state.fees_paise += cost
        self.led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, decision_id=decision_id,
            arm=self.arms[account_id], action=_action_json(action),
            action_hash=action.hash(), policy={"version": self.cfg.policy_version},
            result={"mandate_id": mandate.mandate_id, "rail": rail.value,
                    "status": mandate.status.value, "cost_paise": cost}))

    def _gate(self, action: Action, at: datetime, account_id: str) -> tuple[str, GateDecision]:
        """The only path to a rail. Pass or fail, the verdict is written to the ledger —
        a gate whose denials are invisible proves nothing."""
        decision_id = self._decision_for.pop(account_id, None)
        if decision_id is None:
            self._decision_seq += 1
            decision_id = make_id("dec", self._decision_seq)
        gate = evaluate(action, self.context(account_id, at), self.policy, decision_id)
        self.led.append(EventDraft(
            stage=Stage.GATE, occurred_at=at, account_id=account_id,
            cycle_id=self.world.cycles[account_id].cycle.cycle_id,
            decision_id=decision_id, arm=self.arms[account_id],
            action=_action_json(action), action_hash=action.hash(),
            policy={"version": gate.policy_version, "verdict": gate.verdict.value,
                    "checks_passed": list(gate.rule_ids_passed),
                    "check_failed": gate.rule_id_failed,
                    "reason": gate.reason, "basis": gate.basis},
            model={"planner": self.policy_name, "proposer": "none"}))
        return decision_id, gate

    def notice(self, account_id: str, mandate: Mandate, at: datetime,
               scheduled_for: datetime, split: bool = False) -> None:
        """`split` is the caller saying the debit it is about to schedule will be
        presented in parts, so the notice must describe those parts.

        Deliberately not derived from the cap here. The merchant baselines present the
        full amount whatever the ceiling says — that is what makes them baselines — and
        silently issuing them split notices would improve the control group, which
        understates the agent by exactly the amount of the improvement. Only the agent,
        which is the thing that can actually split, asks for split notices.
        """
        if not self._claim(account_id, ActionType.SEND_PREDEBIT_NOTICE, at):
            return
        state = self.world.cycles[account_id]
        # The notice has to describe the debit that will actually be presented, and the
        # parts come from the same helper the split executor uses — so the notice and the
        # debit cannot disagree about the amount, which is the one thing that would make
        # the 24-hour coupling meaningless.
        parts = (self._parts_for(account_id, mandate) if split
                 else (state.cycle.amount_paise,))
        action = Action(type=ActionType.SEND_PREDEBIT_NOTICE, rail=mandate.rail,
                        amount_paise=state.cycle.amount_paise, scheduled_for=scheduled_for,
                        parts=parts if len(parts) > 1 else None,
                        channel=Channel.SMS, template_id="DLT_RECOVERY_NOTICE_001",
                        cli_series=self.cfg.raw["policy"]["cli_series_service"])
        decision_id, gate = self._gate(action, at, account_id)
        if gate.verdict is not Verdict.ALLOW or self.dry_run:
            return
        receipts = self.world.rails.notify(mandate, action, at, gate)
        state.notices.extend(receipts)
        cost = self.cfg.action_cost_paise(ActionType.SEND_PREDEBIT_NOTICE)
        state.fees_paise += cost
        self.world.contact(account_id, _notice_channel(), at, lifts_intent=False,
                           regulatory=True)
        self.led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, decision_id=decision_id,
            arm=self.arms[account_id], action=_action_json(action),
            action_hash=action.hash(), policy={"version": self.cfg.policy_version},
            result={"notice_id": receipts[0].notice_id if receipts else None,
                    "notice_ids": [r.notice_id for r in receipts],
                    "presentations": list(parts),
                    "cost_paise": cost,
                    "debit_scheduled_for": scheduled_for.isoformat()}))

    def message(self, account_id: str, at: datetime, channel: Channel,
                template_id: str, action_type: ActionType = ActionType.SEND_MESSAGE) -> None:
        """A customer-facing message. The body is rendered from the registered template;
        there is nowhere in `Action` to put text, which is POL-DLT-002 made structural."""
        if not self._claim(account_id, action_type, at):
            return
        state = self.world.cycles[account_id]
        action = Action(type=action_type, channel=channel,
                        template_id=template_id,
                        cli_series=self.cfg.raw["policy"]["cli_series_service"])
        decision_id, gate = self._gate(action, at, account_id)
        if gate.verdict is not Verdict.ALLOW or self.dry_run:
            return
        cost = self.cfg.action_cost_paise(action_type, channel)
        state.fees_paise += cost
        self.world.contact(account_id, channel, at)
        self.led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, decision_id=decision_id,
            arm=self.arms[account_id], action=_action_json(action),
            action_hash=action.hash(), policy={"version": self.cfg.policy_version},
            result={"template_id": template_id, "channel": channel.value,
                    "cost_paise": cost}))

    def request_ptp(self, account_id: str, at: datetime, channel: Channel,
                    action_type: ActionType = ActionType.REQUEST_PTP) -> None:
        """Ask when they can pay. What comes back is information no return code carries —
        the customer's own account of their inflow phase — or a hardship signal.

        `VOICE_CONFIRM_PTP` is the same capture over voice, and it goes through here
        rather than a branch of its own: it had no execution path at all, so the planner
        could select the most intrusive action in the set and have *nothing happen* — no
        gate, no contact, no cost, no harm. A free WAIT wearing a different label, which
        also left POL-AI-001/002 and POL-FREQ-003 unenforceable in a run.
        """
        if not self._claim(account_id, action_type, at):
            return
        state = self.world.cycles[account_id]
        voice = action_type is ActionType.VOICE_CONFIRM_PTP
        action = Action(type=action_type, channel=channel,
                        template_id="DLT_RECOVERY_PTP_001",
                        cli_series=self.cfg.raw["policy"]["cli_series_service"],
                        # POL-AI-001: an automated call opens by saying it is one. Set
                        # here, in deterministic code, never by the proposer.
                        disclosure=True if voice else False)
        decision_id, gate = self._gate(action, at, account_id)
        if gate.verdict is not Verdict.ALLOW or self.dry_run:
            return
        outcome, ptp = self.world.request_ptp(account_id, at, channel)
        cost = self.cfg.action_cost_paise(action_type, channel)
        state.fees_paise += cost
        if voice:
            state.voice_calls += 1
        self.led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, decision_id=decision_id,
            arm=self.arms[account_id], action=_action_json(action),
            action_hash=action.hash(), policy={"version": self.cfg.policy_version},
            result={"outcome": outcome, "cost_paise": cost,
                    "ptp_id": ptp.ptp_id if ptp else None,
                    "promised_date": ptp.promised_date.isoformat() if ptp else None,
                    "confidence": round(ptp.confidence, 3) if ptp else None},
            evidence=(f"ptp_outcome:{outcome}",
                      *( (f"promised_in_days:{(ptp.promised_date - at.date()).days}",)
                         if ptp else ()))))
        if outcome == "DISTRESS":
            # A rule, not a model output: distress language ends the recovery
            # conversation and routes to a human. docs/05-POLICY-ENGINE.md.
            self.close(account_id, at, TerminalState.HARDSHIP,
                       "distress language in the customer's reply; routed out of recovery")

    def _parts_for(self, account_id: str, mandate: Mandate) -> tuple[int, ...]:
        """How this cycle's amount has to be presented on this mandate.

        One part when it fits under the ceiling, several when it does not, `()` when no
        permitted split reaches it. The mandate's own registered cap binds as well as the
        regulatory one — a Rs 20,000 debit on a Rs 8,000 mandate is refused by the issuer
        whatever the AFA rules say.
        """
        from app.domain.money import split_parts

        state = self.world.cycles[account_id]
        category = self.world.accounts[account_id].merchant_category
        regulatory = self.cfg.applicable_cap_paise(mandate.rail, category)
        cap = min(c for c in (regulatory, mandate.cap_paise) if c is not None)
        return split_parts(state.cycle.amount_paise, cap,
                           int(self.cfg.planner["split_max_parts"]))

    def split_retry(self, account_id: str, mandate: Mandate, at: datetime) -> None:
        """Present the cycle in parts, each under the per-transaction ceiling.

        The parts sum to the cycle amount — this collects what is owed, no more and no
        less (POL-AMT-001) — so the split buys nothing against a *balance*: needing the
        whole amount is needing the whole amount. What it buys is the ceiling. Two
        presentations of Rs 10,000 clear a Rs 15,000 AFA-free cap that one of Rs 20,000
        does not, which is the entire reason the action exists and why it is the remedy
        for LIMIT_EXCEEDED rather than a more expensive retry.
        """
        parts = self._parts_for(account_id, mandate)
        state = self.world.cycles[account_id]
        if len(parts) < 2:
            self.unexecutable(account_id, at, ActionType.SPLIT_DEBIT,
                              "no permitted split clears the cap for this amount"
                              if not parts else "the amount fits in one presentation")
            return
        if not self._claim(account_id, ActionType.SPLIT_DEBIT, at):
            return
        action = Action(type=ActionType.SPLIT_DEBIT, rail=mandate.rail,
                        amount_paise=state.cycle.amount_paise, parts=parts,
                        scheduled_for=at)
        decision_id, gate = self._gate(action, at, account_id)
        if gate.verdict is not Verdict.ALLOW or self.dry_run:
            return
        # Same rule as `retry`: never present something we already know has no live
        # notice. For a split that means a notice per part, all of them.
        notices = self.world.rails.notices_for(mandate, parts, at)
        if notices is None:
            return

        results = self.world.rails.attempt_split(mandate, action, at, gate)
        fee = self.cfg.attempt_fee_paise(mandate.rail)
        collected = 0
        for part, result in zip(parts, results):
            state.attempts.append((at, result.rail_code))
            if result.ok:
                collected += part
                state.fees_paise += fee
        state.paid_paise += collected
        settled_at = next((r.settled_at for r in reversed(results)
                           if r.ok and r.settled_at), None)
        # Partial collection is a real outcome, not a rounding error: part one lands,
        # part two is refused, and the cycle is neither settled nor untouched. `settled`
        # keeps meaning the whole amount arrived.
        full = state.paid_paise >= state.cycle.amount_paise
        if full and settled_at:
            self.world.settle(account_id, settled_at, "rail")
        self.led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, decision_id=decision_id,
            arm=self.arms[account_id], action=_action_json(action),
            action_hash=action.hash(), policy={"version": self.cfg.policy_version},
            notice_ref=notices[0].notice_id if notices else None,
            result={"ok": full,
                    "rail_code": results[0].rail_code if results else "",
                    "parts": list(parts),
                    "notice_ids": [n.notice_id for n in notices],
                    "part_codes": [r.rail_code for r in results],
                    "presented": len(results),
                    "collected_paise": collected,
                    "paid_paise": state.paid_paise,
                    "settled_at": settled_at.isoformat() if settled_at else None,
                    "cost_paise": sum(fee for r in results if r.ok)},
            evidence=(f"parts:{len(parts)}",
                      f"collected_paise:{collected}",
                      *(f"part_code:{r.rail_code}" for r in results))))

    def retry(self, account_id: str, mandate: Mandate, at: datetime) -> None:
        if not self._claim(account_id, ActionType.RETRY_DEBIT, at):
            return
        state = self.world.cycles[account_id]
        action = Action(type=ActionType.RETRY_DEBIT, rail=mandate.rail,
                        amount_paise=state.cycle.amount_paise, scheduled_for=at)
        decision_id, gate = self._gate(action, at, account_id)
        if gate.verdict is not Verdict.ALLOW or self.dry_run:
            return
        notice = self.world.rails.notice_for(mandate, state.cycle.amount_paise, at)
        if notice is None:
            # Presenting a debit we know has no live notice would be rejected at the rail
            # and counted as our own defect. A scheduler that does this has a bug; not
            # doing it is what keeps notice_window_violations at zero.
            return
        result = self.world.rails.attempt(mandate, action, at, gate)
        state.attempts.append((at, result.rail_code))
        cost = self.cfg.attempt_fee_paise(mandate.rail) if result.ok else 0
        state.fees_paise += cost
        if result.ok and result.settled_at:
            self.world.settle(account_id, result.settled_at, "rail")
        self.led.append(EventDraft(
            stage=Stage.EXECUTE, occurred_at=at, account_id=account_id,
            cycle_id=state.cycle.cycle_id, decision_id=decision_id,
            arm=self.arms[account_id], action=_action_json(action),
            action_hash=action.hash(), policy={"version": self.cfg.policy_version},
            notice_ref=notice.notice_id if notice else None,
            result={"ok": result.ok, "rail_code": result.rail_code,
                    "notice_ids": [notice.notice_id],
                    "settled_at": result.settled_at.isoformat() if result.settled_at else None,
                    "fee_paise": result.fee_paise, "cost_paise": cost}))


def _resolve_promises(world: World, led: Ledger, arms: dict[str, Arm], executor,
                      day: date, cfg: Config) -> None:
    """Resolve every promise that has come due, automatically, against settlement.

    A promise nobody checks is a CRM note. Resolution reads what actually arrived, writes
    the transition to the ledger, and lets the consequences follow — a kept promise
    restores the contact budget, a broken one costs the account trust in its next one.
    """
    from app.domain.ptp import due

    grace = cfg.policy.ptp_grace_days
    for account_id, state in world.cycles.items():
        if not due(state.ptp, day, grace):
            continue
        before = state.ptp_confidence
        outcome = world.resolve_ptp(account_id, day)
        if outcome is None:
            continue
        status, paid = outcome
        led.append(EventDraft(
            stage=Stage.OBSERVE, occurred_at=datetime.combine(day, time(12), tzinfo=IST),
            account_id=account_id, cycle_id=state.cycle.cycle_id,
            arm=arms[account_id],
            result={"ptp_status": status.value, "ptp_id": state.ptp.ptp_id,
                    "paid_paise": paid,
                    "confidence_before": round(before, 3),
                    "confidence_after": round(state.ptp_confidence, 3)},
            evidence=(f"ptp_status:{status.value}",
                      f"promised_date:{state.ptp.promised_date.isoformat()}",
                      f"paid_share:{paid / max(1, state.cycle.amount_paise):.2f}")))


def _observe_settlements(world: World, led: Ledger, arms: dict[str, Arm], day: date,
                         seen: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Read the settlement feed and record what actually cleared.

    These OBSERVE events deliberately carry no `ok` key, so they leave `settled` NULL in
    the index and cannot double-count an account in the scoreboard's SUM. The single
    settled-carrying row per cycle is still the one written at CLOSE. What these add is
    *when* the money arrived, which is where days-to-recover comes from.
    """
    day_end = datetime.combine(day, time(23, 59), tzinfo=IST)
    for s in world.rails.settlement_feed(datetime.combine(world.start, time(0), tzinfo=IST)):
        key = (s.account_id, s.cycle_id)
        if key in seen or s.settled_at > day_end:
            continue
        seen.add(key)
        led.append(EventDraft(
            stage=Stage.OBSERVE, occurred_at=s.settled_at, account_id=s.account_id,
            cycle_id=s.cycle_id, arm=arms[s.account_id],
            result={"settled_at": s.settled_at.isoformat(), "source": s.source,
                    "amount_paise": s.amount_paise},
            evidence=(f"settlement_source:{s.source}",
                      f"day_of_cycle:{(s.settled_at.date() - world.start).days}")))
    return seen


def _action_json(action: Action) -> dict:
    return {k: (v.value if hasattr(v, "value") else
                v.isoformat() if isinstance(v, datetime) else v)
            for k, v in action.__dict__.items() if v is not None and v is not False}


def _notice_channel():
    from app.domain.enums import Channel
    return Channel.SMS
