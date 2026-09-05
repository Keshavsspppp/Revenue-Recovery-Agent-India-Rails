"""The simulated world: accounts, mandates, cycles, latent state, and the day loop.

`World` owns the state and the mechanics: balance dynamics, contact effects, hazards, and
the rails. It does not own any *policy* — the merchant default that the holdout arm
receives is implemented once, in `app.runner`, through the real compliance gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

import numpy as np

from app.domain.clock import IST
from app.domain.codemap import CodeMap, load_codemap
from app.domain.config import Config
from app.domain.enums import (
    ActionType,
    CauseClass,
    Channel,
    MandateStatus,
    MerchantCategory,
    PTPStatus,
    Rail,
    TerminalState,
    Verdict,
)
from app.domain.models import (
    Account,
    Action,
    BillingCycle,
    ConsentState,
    GateDecision,
    Mandate,
    NoticeReceipt,
    PromiseToPay,
    make_id,
)
from app.domain.ptp import next_confidence, resolve
from app.sim.latent import (
    LatentAccount,
    apply_contact,
    burn_in,
    contact_hazards,
    generate_latent,
    selfpay_hazard,
)
from app.sim.rails import Settlement, SimRailAdapter

#: Debit attempts run mid-morning; notices go out the evening before they are due.
ATTEMPT_TIME = time(10, 0)
NOTICE_TIME = time(9, 0)


def provisional_gate(action: Action, at: datetime, policy_version: str) -> GateDecision:
    """A stand-in ALLOW so the simulator can drive rails before the policy engine exists.

    M5 replaces every call site with `app.policy.evaluate`. Until then this is the only
    thing that can produce an ALLOW, and it is confined to `app/sim/` — no agent code
    path can reach it. `tests/test_no_provisional_gate_outside_sim` guards that.
    """
    return GateDecision(
        decision_id="dec_provisional", action_hash=action.hash(), verdict=Verdict.ALLOW,
        rule_ids_passed=(), rule_id_failed=None, reason=None,
        policy_version=policy_version, evaluated_at=at)


@dataclass
class CycleState:
    """Everything observable about one account's recovery attempt, plus its outcome."""

    cycle: BillingCycle
    attempts: list[tuple[datetime, str]] = field(default_factory=list)
    notices: list[NoticeReceipt] = field(default_factory=list)
    #: Every touch the customer receives, notices included. This is what the harm
    #: scoreboard reports, because from their side a notice is a message.
    contacts: list[datetime] = field(default_factory=list)
    #: Discretionary recovery outreach only. This is what POL-FREQ-001/002 cap, for the
    #: same reason POL-QH-001 exempts the notice: the frequency rules govern recovery
    #: contact, and a mandatory regulatory notification must not consume the budget for
    #: it. Otherwise complying with one rule silently spends your allowance under another.
    recovery_contacts: list[datetime] = field(default_factory=list)
    #: Voice calls placed this cycle. POL-FREQ-003 caps it, and a cap needs a
    #: counter — the gate context used to report the full budget every time.
    voice_calls: int = 0
    settled_at: datetime | None = None
    settled_source: str | None = None       # "rail" | "self_pay"
    terminal: TerminalState | None = None
    opted_out: bool = False
    complained: bool = False
    disputed: bool = False
    mandate_cancelled: bool = False
    fees_paise: int = 0
    first_failure_code: str = ""
    #: Dates on which past cycles collected cleanly. Observable history — this is what
    #: the planner infers the inflow phase from, and it is the *only* thing it gets.
    prior_successes: tuple[date, ...] = ()
    ptp: PromiseToPay | None = None
    #: Money collected against this cycle. A partially kept promise moves this without
    #: settling the cycle, so `settled` still means the whole amount arrived.
    paid_paise: int = 0
    ptp_confidence: float = 0.6
    distress_signalled: bool = False
    #: A second merchant's mandate failing in the same window. Observable to a PSP, which
    #: sees debits across merchants, and one of the few hardship signals that does not
    #: require the customer to tell us anything.
    other_mandate_failing: bool = False

    @property
    def open(self) -> bool:
        """The agent may still act on this cycle."""
        return self.settled_at is None and self.terminal is None

    @property
    def unsettled(self) -> bool:
        """The money has not arrived yet — so the customer may still pay it.

        Deliberately *not* the same as `open`. Ending the recovery workflow means the
        agent stops spending on the account; it does not mean the customer stops being
        able to pay. Conflating the two would penalise the agent for stopping, by
        deleting the free self-cure it was right to stop competing with.
        """
        return self.settled_at is None


@dataclass
class World:
    """Seeded, deterministic, and the only holder of latent truth."""

    cfg: Config
    codemap: CodeMap
    seed: int
    start: date
    accounts: dict[str, Account] = field(default_factory=dict)
    latent: dict[str, LatentAccount] = field(default_factory=dict)
    mandates: dict[str, list[Mandate]] = field(default_factory=dict)
    mandates_by_id: dict[str, Mandate] = field(default_factory=dict)
    cycles: dict[str, CycleState] = field(default_factory=dict)
    settlements: list[Settlement] = field(default_factory=list)
    #: One independent stream per account. Every stochastic draw that affects an account
    #: — its balance, its hazards, its rail outcomes — comes from *its own* generator.
    #:
    #: A single shared stream silently couples the arms: when the treatment policy acts
    #: more often it consumes more draws, which shifts every subsequent draw for holdout
    #: accounts too. The control group's outcomes then depend on what the treatment arm
    #: did, which is the one thing a control group must not do. Measured: the holdout
    #: rate moved 31.8%-41.1% across three policies that treat it identically.
    rngs: dict[str, np.random.Generator] = field(default_factory=dict)
    rng_hazard: np.random.Generator = None            # type: ignore[assignment]
    rails: SimRailAdapter = None                      # type: ignore[assignment]

    # ---- construction --------------------------------------------------------

    @classmethod
    def generate(cls, cfg: Config, seed: int, n_accounts: int,
                 start: date | None = None, codemap: CodeMap | None = None) -> World:
        """One Generator per batch, split into named sub-streams so that adding a hazard
        later does not reshuffle account generation and invalidate a reported run."""
        sim = cfg.sim
        start = start or date(2026, 9, 1)
        rng_accounts, rng_rails, rng_hazard = np.random.default_rng(seed).spawn(3)
        world = cls(cfg=cfg, codemap=codemap or load_codemap(), seed=seed, start=start,
                    rng_hazard=rng_hazard)
        world.rails = SimRailAdapter(cfg, world.codemap, rng_rails, world)

        cat_names = list(sim["category_mix"])
        cat_p = [sim["category_mix"][c] for c in cat_names]
        tier_names = list(sim["city_tier_mix"])
        tier_p = [sim["city_tier_mix"][t] for t in tier_names]
        rail_names = list(sim["rail_mix"])
        rail_p = [sim["rail_mix"][r] for r in rail_names]
        cause_names = list(sim["defect_mix"])
        cause_p = [sim["defect_mix"][c] for c in cause_names]

        for i in range(1, n_accounts + 1):
            r = rng_accounts
            category = MerchantCategory(cat_names[r.choice(len(cat_p), p=cat_p)])
            tier = int(tier_names[r.choice(len(tier_p), p=tier_p)])
            rail = Rail(rail_names[r.choice(len(rail_p), p=rail_p)])
            cause = CauseClass(cause_names[r.choice(len(cause_p), p=cause_p)])

            account_id = make_id("acc", i)
            # Seeded from (batch seed, account index): independent of every other
            # account, and identical whatever policy is later run over the batch.
            world.rngs[account_id] = np.random.default_rng([seed, 0xACC, i])
            latent = generate_latent(r, sim, tier)
            burn_in(latent, world.rngs[account_id], sim, start)

            lo, hi = sim["amount_rupees_by_category"][category.value]
            amount = int(np.exp(r.uniform(np.log(lo), np.log(hi))) * 100)

            registered_at = datetime.combine(
                start - timedelta(days=int(r.integers(20, 400))), time(12), tzinfo=IST)
            world.accounts[account_id] = Account(
                account_id=account_id, merchant_category=category, city_tier=tier,
                consent=_consent(r, registered_at),
                created_at=datetime.combine(start, time(0), tzinfo=IST))
            world.latent[account_id] = latent

            cap_lo, cap_hi = sim["mandate_cap_multiple"]
            mandate = Mandate(
                mandate_id=make_id("mnd", i), account_id=account_id, rail=rail,
                cap_paise=int(amount * r.uniform(cap_lo, cap_hi)),
                status=MandateStatus.ACTIVE, registered_at=registered_at)
            world.add_mandate(mandate)
            if r.random() < sim["second_mandate_p"]:
                alt = [x for x in Rail if x not in (rail, Rail.PAYMENT_LINK)]
                world.add_mandate(Mandate(
                    mandate_id=make_id("mnd", 500_000 + i), account_id=account_id,
                    rail=alt[r.choice(len(alt))], cap_paise=int(amount * r.uniform(cap_lo, cap_hi)),
                    status=MandateStatus.ACTIVE,
                    registered_at=datetime.combine(start - timedelta(days=int(r.integers(20, 400))),
                                                   time(12), tzinfo=IST)))

            cycle = BillingCycle(cycle_id=make_id("cyc", i), account_id=account_id,
                                 amount_paise=amount, due_date=start,
                                 horizon_days=cfg.horizon_days)
            other_p = sim["other_mandate_failing_p"]
            world.cycles[account_id] = CycleState(
                cycle=cycle,
                prior_successes=_prior_successes(latent, sim, start,
                                                 world.rngs[account_id]),
                other_mandate_failing=bool(
                    world.rngs[account_id].random()
                    < other_p["hardship" if latent.hardship else "other"]))
            world._stage_first_failure(account_id, cause, r)

        return world

    def _stage_first_failure(self, account_id: str, cause: CauseClass,
                             rng: np.random.Generator) -> None:
        """Construct the state that produces the intended first-failure cause.

        The defect mix in docs/03-SIMULATOR.md is a *generation* parameter and a stated
        calibration target, so the cause is drawn first and the world is arranged to
        realise it — rather than drawn from whatever the rails happened to emit.
        """
        state = self.cycles[account_id]
        latent = self.latent[account_id]
        mandate = self.mandates[account_id][0]
        amount = state.cycle.amount_paise

        if cause is CauseClass.INSUFFICIENT_FUNDS:
            latent.balance_paise = int(amount * rng.uniform(0.0, 0.95))
        elif cause is CauseClass.LIMIT_EXCEEDED:
            self._replace_mandate(mandate, cap_paise=int(amount * rng.uniform(0.4, 0.9)))
        elif cause in (CauseClass.MANDATE_INVALID, CauseClass.ACCOUNT_TERMINAL):
            latent.mandate_defect = cause
            self._replace_mandate(mandate, status=MandateStatus.INVALID, defect=cause)
        elif cause is CauseClass.MANDATE_REVOKED:
            self._replace_mandate(mandate, status=MandateStatus.REVOKED)
        elif cause is CauseClass.AUTH_ARTEFACT:
            self._replace_mandate(mandate, status=MandateStatus.PENDING_AFA)
        elif cause is CauseClass.TRANSIENT_INFRA:
            latent.balance_paise = max(latent.balance_paise, amount)
        # The true cause is latent; the reported code may disagree with it.
        latent.true_cause = cause
        state.first_failure_code = self.rails.reported_code(
            self.mandates[account_id][0].rail, cause, self.rngs[account_id])
        state.attempts.append((datetime.combine(self.start, ATTEMPT_TIME, tzinfo=IST),
                               state.first_failure_code))

    def _replace_mandate(self, mandate: Mandate, **changes: Any) -> Mandate:
        """Mandates are frozen; replacing is how they change state."""
        updated = Mandate(**{**mandate.__dict__, **changes})
        self.mandates[mandate.account_id] = [
            updated if m.mandate_id == mandate.mandate_id else m
            for m in self.mandates[mandate.account_id]]
        self.mandates_by_id[mandate.mandate_id] = updated
        return updated

    def add_mandate(self, mandate: Mandate) -> None:
        self.mandates.setdefault(mandate.account_id, []).append(mandate)
        self.mandates_by_id[mandate.mandate_id] = mandate

    def merchant_name(self, account_id: str) -> str:
        return f"{self.accounts[account_id].merchant_category.value.title()} Merchant"

    # ---- daily mechanics -----------------------------------------------------

    def open_accounts(self) -> list[str]:
        """Cycles the agent may still act on."""
        return [a for a, s in self.cycles.items() if s.open]

    def unsettled_accounts(self) -> list[str]:
        """Cycles whose money has not arrived, closed workflow or not."""
        return [a for a, s in self.cycles.items() if s.unsettled]

    def rng(self, account_id: str) -> np.random.Generator:
        return self.rngs[account_id]

    def tick_day(self, day: date) -> None:
        for account_id, state in self.cycles.items():
            if state.unsettled:
                self.latent[account_id].tick(day, self.rngs[account_id], self.cfg.sim)
                self.resolve_pending_afa(account_id, day)

    def resolve_pending_afa(self, account_id: str, day: date) -> bool:
        """Some customers actually go and authorise the mandate we asked them to.

        Without this a repaired mandate stays PENDING_AFA for ever, so REREGISTER_MANDATE
        could never pay off — while the planner priced it at a 55% completion chance. The
        agent was buying an option the world had no way of honouring, and the account
        timeline showed it plainly: repair, repair again, close as worthless.

        Scaled by responsiveness, because completing an authorisation is the same
        willingness to act that drives every other customer response here.
        """
        cfg = self.cfg.sim.get("afa")
        if not cfg:
            return False
        latent = self.latent[account_id]
        rng = self.rngs[account_id]
        for mandate in list(self.mandates.get(account_id, ())):
            if mandate.status is not MandateStatus.PENDING_AFA:
                continue
            age = (day - mandate.registered_at.date()).days
            if age < 1 or age > int(cfg["window_days"]):
                continue
            # Spread the window's completion probability across its days.
            per_day = float(cfg["completion_p"]) * latent.responsiveness / int(cfg["window_days"])
            if rng.random() < per_day:
                self._replace_mandate(mandate, status=MandateStatus.ACTIVE)
                return True
        return False

    def settle(self, account_id: str, at: datetime, source: str) -> None:
        state = self.cycles[account_id]
        state.settled_at = at
        state.settled_source = source
        state.terminal = TerminalState.RECOVERED
        self.settlements.append(Settlement(account_id, state.cycle.cycle_id,
                                           state.cycle.amount_paise, at, source))

    def roll_self_pay(self, account_id: str, day: date) -> bool:
        """The customer pays through some other route. One of the two mechanisms that
        make the holdout non-trivial; the other is the merchant's own default retries."""
        state = self.cycles[account_id]
        latent = self.latent[account_id]
        window = timedelta(days=self.cfg.sim["notified_recently_days"])
        at = datetime.combine(day, time(18, 0), tzinfo=IST)
        notified = any(at - n.issued_at <= window for n in state.notices)
        p = selfpay_hazard(latent, state.cycle.amount_paise, notified, self.cfg.sim)
        if self.rngs[account_id].random() >= p:
            return False
        latent.balance_paise = max(0, latent.balance_paise - state.cycle.amount_paise)
        self.settle(account_id, at, "self_pay")
        return True

    def request_ptp(self, account_id: str, at: datetime,
                    channel: Channel) -> tuple[str, PromiseToPay | None]:
        """Ask the customer when they can pay, and see what comes back.

        Three outcomes worth distinguishing: a date (which is the customer telling you
        their own inflow phase, information no return code carries), distress language
        (which is a hardship signal and must route out of recovery rather than into
        another attempt), or silence.
        """
        state, latent = self.cycles[account_id], self.latent[account_id]
        cfg = self.cfg.sim["ptp"]
        rng = self.rngs[account_id]
        self.contact(account_id, channel, at)
        if state.terminal is not None:            # the contact itself may have ended it
            return "NO_REPLY", None
        if rng.random() >= float(cfg["reply_lift"]) * latent.responsiveness:
            return "NO_REPLY", None
        distress_p = (self.cfg.sim["distress_phrase_p"] if latent.hardship
                      else self.cfg.sim["distress_phrase_p_other"])
        if rng.random() < distress_p:
            # A rule, not a model output: distress ends the recovery conversation.
            state.distress_signalled = True
            return "DISTRESS", None
        if rng.random() >= float(cfg["capture_given_reply"]):
            return "NO_PROMISE", None

        lag_lo, lag_hi = cfg["promise_lag_days"]
        days_ahead = (latent.inflow_day - at.day) % 30 + int(rng.integers(lag_lo, lag_hi + 1))
        promised = at.date() + timedelta(days=max(1, min(days_ahead, 20)))
        ptp = PromiseToPay(
            ptp_id=make_id("ptp", abs(hash((account_id, promised.isoformat()))) % 10**8),
            account_id=account_id, cycle_id=state.cycle.cycle_id,
            amount_paise=state.cycle.amount_paise, promised_date=promised,
            channel=channel, captured_by="REQUEST_PTP",
            confidence=state.ptp_confidence, status=PTPStatus.OPEN)
        state.ptp = ptp
        return "PTP_CAPTURED", ptp

    def resolve_ptp(self, account_id: str, day: date) -> tuple[PTPStatus, int] | None:
        """Resolve a due promise against settlement. Automatic, on the date, from the
        evidence — nobody decides a promise was kept."""
        state = self.cycles[account_id]
        ptp = state.ptp
        if ptp is None or ptp.status is not PTPStatus.OPEN:
            return None
        cfg = self.cfg.sim["ptp"]
        rng = self.rngs[account_id]
        latent = self.latent[account_id]
        outstanding = state.cycle.amount_paise - state.paid_paise

        # Did they do what they said? Having the money is necessary but not sufficient.
        if state.settled_at is None and latent.balance_paise >= outstanding:
            if rng.random() < float(cfg["keep_given_funds"]) * ptp.confidence:
                latent.balance_paise -= outstanding
                state.paid_paise = state.cycle.amount_paise
                self.settle(account_id, datetime.combine(day, time(12), tzinfo=IST),
                            "ptp_kept")
        elif state.settled_at is None and rng.random() < float(cfg["partial_given_short"]):
            lo, hi = cfg["partial_fraction"]
            part = min(latent.balance_paise, int(outstanding * rng.uniform(lo, hi)))
            latent.balance_paise -= part
            state.paid_paise += part

        status = resolve(ptp, state.paid_paise, state.cycle.amount_paise,
                         cycle_ended=day > state.cycle.due_date + timedelta(
                             days=state.cycle.horizon_days))
        state.ptp = PromiseToPay(**{**ptp.__dict__, "status": status})
        state.ptp_confidence = next_confidence(
            state.ptp_confidence, status, float(cfg["broken_decay"]),
            float(cfg["kept_recovery"]))
        if status is PTPStatus.KEPT:
            # A kept promise earns the fatigue budget back: this customer engaged, and
            # treating them as though they had ignored you is how you lose them next time.
            state.recovery_contacts.clear()
        return status, state.paid_paise

    def lapse_ptp(self, account_id: str) -> PTPStatus | None:
        """The cycle ended while a promise was still open.

        The customer never reached the date they named, so this is LAPSED, not BROKEN —
        and it leaves their trust untouched, because it says nothing about them.
        """
        state = self.cycles[account_id]
        if state.ptp is None or state.ptp.status is not PTPStatus.OPEN:
            return None
        status = resolve(state.ptp, state.paid_paise, state.cycle.amount_paise,
                         cycle_ended=True)
        state.ptp = PromiseToPay(**{**state.ptp.__dict__, "status": status})
        return status

    def contact(self, account_id: str, channel: Channel, at: datetime,
                lifts_intent: bool = True, regulatory: bool = False) -> None:
        """Apply a customer contact and roll its hazards. The only path by which intent
        and annoyance move — and it never touches balance.

        `regulatory` marks the pre-debit notice: it still annoys, still counts on the
        harm scoreboard, and still lifts self-pay, but it does not spend the frequency
        budget that governs discretionary outreach.
        """
        state, latent = self.cycles[account_id], self.latent[account_id]
        state.contacts.append(at)
        if not regulatory:
            state.recovery_contacts.append(at)
        apply_contact(latent, channel, lifts_intent, self.cfg.sim)
        h = contact_hazards(latent, self.cfg.sim)
        rng = self.rngs[account_id]
        if rng.random() < h["opt_out"]:
            state.opted_out = True
            state.terminal = TerminalState.OPTED_OUT
            # The gate reads the withdrawal off the *consent* record, not off this flag,
            # so recording it only here left POL-STOP-001 structurally unable to fire in
            # any run: an opted-out customer was stopped by "this cycle already has a
            # terminal state", which is a different rule with a different basis and would
            # not have stopped anything had the cycle still been open.
            account = self.accounts[account_id]
            self.accounts[account_id] = replace(
                account, consent=replace(account.consent, opted_out_at=at))
        if rng.random() < h["complaint"]:
            state.complained = True
        if rng.random() < h["dispute"]:
            state.disputed = True
            state.terminal = TerminalState.DISPUTED
        if rng.random() < h["mandate_cancel"]:
            # Worse than an opt-out: this ends future revenue, not just this cycle.
            state.mandate_cancelled = True
            for m in self.mandates.get(account_id, ()):
                if m.status is MandateStatus.ACTIVE:
                    self._replace_mandate(m, status=MandateStatus.REVOKED)
                    break

    # ---- rail operations, always through the gate ----------------------------

    def send_notice(self, account_id: str, mandate: Mandate, at: datetime,
                    scheduled_for: datetime,
                    gate_fn: Callable[[Action, datetime], GateDecision | None],
                    ) -> NoticeReceipt | None:
        state = self.cycles[account_id]
        action = Action(type=ActionType.SEND_PREDEBIT_NOTICE, rail=mandate.rail,
                        amount_paise=state.cycle.amount_paise, scheduled_for=scheduled_for)
        gate = gate_fn(action, at)
        if gate is None or gate.verdict is not Verdict.ALLOW:
            return None
        receipts = self.rails.notify(mandate, action, at, gate)
        state.notices.extend(receipts)
        receipt = receipts[0] if receipts else None
        state.fees_paise += self.cfg.action_cost_paise(ActionType.SEND_PREDEBIT_NOTICE)
        # A notice is a contact in the harm sense — it is mandatory, so it is priced low,
        # but it informs the customer and measurably lifts self-pay. It does not ask them
        # for anything, so it does not lift intent.
        self.contact(account_id, Channel.SMS, at, lifts_intent=False)
        return receipt

    def primary_mandate(self, account_id: str) -> Mandate | None:
        """The mandate this cycle's debit is registered against. The merchant default
        uses *this* one and only this one: silently failing over to another rail would
        make the do-nothing baseline perform multi-rail repair, which is the agent's
        job and would contaminate the holdout arm."""
        mandates = self.mandates.get(account_id)
        return mandates[0] if mandates else None

    def active_mandate(self, account_id: str) -> Mandate | None:
        """Any live mandate, on any rail. Only the agent may use this — choosing a
        different rail is an intervention, not a default."""
        for m in self.mandates.get(account_id, ()):
            if m.status is MandateStatus.ACTIVE:
                return m
        return self.mandates.get(account_id, [None])[0]


def _prior_successes(latent: LatentAccount, sim: Any, start: date,
                     rng: np.random.Generator) -> tuple[date, ...]:
    """Past cycles that collected. The merchant attempts on a fixed due date, but the
    attempt that *succeeds* lands once the money is there — so these dates cluster a few
    days after the customer's inflow, which is exactly the phase signal the planner needs
    and the only one it is allowed."""
    lo, hi = sim["prior_success_count"]
    lag_lo, lag_hi = sim["prior_success_lag_days"]
    months = int(rng.integers(lo, hi + 1))
    out: list[date] = []
    for m in range(1, months + 1):
        day = latent.inflow_day + int(rng.integers(lag_lo, lag_hi + 1))
        month = start.month - m
        year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        out.append(date(year, month, min(day, 28)))
    return tuple(sorted(out))


def _consent(rng: np.random.Generator, registered_at: datetime) -> ConsentState:
    channels = {Channel.SMS}
    if rng.random() < 0.72:
        channels.add(Channel.WHATSAPP)
    if rng.random() < 0.85:
        channels.add(Channel.EMAIL)
    if rng.random() < 0.55:
        channels.add(Channel.PUSH)
    if rng.random() < 0.40:
        channels.add(Channel.VOICE)
    return ConsentState(channels_allowed=frozenset(channels),
                        dnd_registered=bool(rng.random() < 0.28),
                        recording_consent=bool(rng.random() < 0.5),
                        # Registering a mandate *is* an additional-factor authentication.
                        # Whether it is still fresh is what POL-AFA-002 turns on, so an
                        # account with an old mandate has to be re-consented before its
                        # mandate can be repaired onto another rail.
                        afa_authorised_at=registered_at)


# The merchant default policy used to live here as `run_merchant_default`, driving rails
# through `provisional_gate`. It was deleted at M5: `app.runner.run_batch` now implements
# that policy once, through the real compliance gate. Two implementations of the holdout
# arm's behaviour is precisely how a control group stops meaning anything — the baseline
# has to be the same code the treatment arm's plumbing runs on.


def at_risk_paise(world: World) -> int:
    return sum(s.cycle.amount_paise for s in world.cycles.values())


def recovered_paise(world: World) -> int:
    return sum(s.cycle.amount_paise for s in world.cycles.values() if s.settled_at)
