"""The Groq proposer. OpenAI-compatible, structured outputs, strict mode.

Be honest about why this is here. The LLM does **not** improve retry timing — the planner
does that with arithmetic, and better. What it plausibly adds is reading unstructured
signals into structured evidence, choosing among several near-equal-EV actions using
context the state vector does not encode, and writing the rationale that goes in the audit
trail. Whether it adds any of that is measured in `app/eval/ablate.py`, and "it did not"
is a reportable answer rather than a failure to hide.

Bounded by construction:

  * one retry, then fall through. An agent that re-prompts until it likes the answer has
    no bound on cost or latency.
  * a proposal outside the eligible set is discarded, logged `PROPOSER_INVALID`, and the
    planner decides instead.
  * the payload carries no name, phone, email, account number or city — and an amount
    *band* rather than an amount. DPDP data minimisation, made structural: the model
    cannot leak a rupee figure it was never given.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from app.domain.enums import ActionType, CauseClass
from app.domain.money import amount_band
from app.propose.schema import SYSTEM_PROMPT, Proposal, parse, response_format

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"

#: Cloudflare in front of the API rejects urllib's default `Python-urllib/3.x` outright —
#: HTTP 403, Cloudflare error 1010, identically with and without a valid key, so it never
#: reaches Groq's auth at all. Identifying the client properly is the fix, and it is good
#: manners regardless.
USER_AGENT = "revenue-recovery/0.1 (+https://github.com/; python-urllib)"


class ProposerUnavailable(Exception):
    """No API key. Not an error — the deterministic proposer is the control arm."""


def _post(url: str, body: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    """One POST, via the standard library. A single JSON call does not justify a
    dependency, and adding one would mean the repo stops running where it cannot be
    installed."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        # The server states its own budget on every reply. Carrying it back means the
        # throttle is read rather than guessed — guessing it three times is what made
        # two earlier ablations measure the rate limiter instead of the model.
        payload["_ratelimit"] = {
            "remaining_tokens": response.headers.get("x-ratelimit-remaining-tokens"),
            "reset_tokens": response.headers.get("x-ratelimit-reset-tokens")}
        return payload


#: 429s that will not clear inside a run. A per-minute limit is worth waiting out; a
#: per-day one is a wall, and retrying into it produces a column of failures that reads
#: exactly like a model declining to answer.
DAILY_LIMIT_MARKERS = ("per day (tpd)", "per day (rpd)", "tokens per day",
                       "requests per day")


def _reset_seconds(value: str | None) -> float | None:
    """Groq states resets as `615ms`, `5.505s`, `2h54m14.4s`."""
    if not value:
        return None
    total, number = 0.0, ""
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    i = 0
    while i < len(value):
        ch = value[i]
        if ch.isdigit() or ch == ".":
            number += ch
        elif value[i:i + 2] == "ms":
            total += float(number or 0) / 1000.0
            number = ""
            i += 1
        elif ch in units:
            total += float(number or 0) * units[ch]
            number = ""
        i += 1
    return total if total or number == "" else float(number)


@dataclass
class GroqProposer:
    """Transport is injectable so the retry, validation and caching logic can be tested
    without a key or a network."""

    api_key: str | None = None
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 6.0
    max_retries: int = 1
    #: Minimum seconds between calls, derived from the limits the API actually reports
    #: rather than guessed:
    #:
    #:     x-ratelimit-limit-tokens: 8000    (per minute)
    #:     measured cost per call:    932    (555 prompt + 377 completion)
    #:     8000 / 932 = 8.6 calls/min -> one every ~7s
    #:
    #: This matters more than it looks. Unthrottled, a 120-account run produced 232 HTTP
    #: 429s out of 233 calls and every one fell through to the planner — which made the
    #: LLM arm of the ablation byte-identical to the planner arm and looked exactly like
    #: "the LLM adds nothing". A throttled client measures the model; an unthrottled one
    #: measures the rate limiter, and the two are indistinguishable in the results table.
    #: At 2.1s it was still 72% 429s. Guessing twice is what made it worth measuring.
    min_interval_s: float = 7.5
    #: gpt-oss reasons before it answers, and the reasoning is what the call costs. At
    #: default effort one call reserved 729 + 800 = 1,529 tokens; at "low" it is 681 +
    #: 400 = 1,081, and the measured completion was 297 of that 400. On a 200,000
    #: tokens-per-day tier that is the difference between 130 decisions and 185.
    reasoning_effort: str | None = "low"
    max_output_tokens: int = 400
    _last_call_at: float = 0.0
    transport: Callable[[str, dict[str, Any], str, float], dict[str, Any]] = _post

    calls: int = 0
    cache_hits: int = 0
    invalid: int = 0
    failures: int = 0
    #: Why calls failed, by kind. A bare failure count is not enough to read an ablation:
    #: a run where the model declined and a run where the transport was rate-limited look
    #: identical in the results and mean completely different things.
    failure_kinds: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[int] = field(default_factory=list)
    _cache: dict[str, Proposal] = field(default_factory=dict)
    #: Set when a 429 names a per-*day* limit. Not an error to retry — a wall for the
    #: rest of the run. Every arm that keeps calling into it reports a column of
    #: failures indistinguishable from a model that declined to answer.
    exhausted: str | None = None
    _remaining_tokens: int | None = None
    _reset_tokens_s: float | None = None
    _last_reservation: int = 0

    @property
    def name(self) -> str:
        return f"groq:{self.model}"

    @classmethod
    def from_env(cls, **overrides: Any) -> GroqProposer:
        # Deliberately does *not* read .env. `app.cli.main` loads it once at entry, and
        # a constructor that quietly repopulates the environment defeats a test's
        # monkeypatch.delenv — which is not a style point: it let the test suite restore
        # a real key and start making paid API calls, and the run hung rather than failed.
        key = os.environ.get("GROQ_API_KEY") or None
        if not key:
            raise ProposerUnavailable(
                "GROQ_API_KEY is not set. The deterministic proposer runs instead, and "
                "the batch is labelled proposer=rules — that is the control arm, not a "
                "degraded mode.")
        return cls(api_key=key,
                   model=os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
                   timeout_s=float(os.environ.get("GROQ_TIMEOUT_S", 6)),
                   max_retries=int(os.environ.get("GROQ_MAX_RETRIES", 1)),
                   # 2.1 was the guess that still produced 72% 429s; the field's
                   # own default is derived from the reported limits. Defaulting
                   # to the stale guess here silently reinstates the failure the
                   # field comment exists to prevent.
                   min_interval_s=float(os.environ.get(
                       "GROQ_MIN_INTERVAL_S", cls.min_interval_s)),
                   reasoning_effort=os.environ.get("GROQ_REASONING_EFFORT",
                                                   cls.reasoning_effort) or None,
                   max_output_tokens=int(os.environ.get("GROQ_MAX_OUTPUT_TOKENS",
                                                        cls.max_output_tokens)),
                   **overrides)

    # ---- the payload the model sees ------------------------------------------

    @staticmethod
    def user_payload(plan_state, posterior: dict[CauseClass, float],
                     eligible: frozenset[ActionType], amount_paise: int,
                     inflow, mandates, hardship_score: float = 0.0,
                     merchant_category: str = "") -> dict[str, Any]:
        """A state vector, not a person. No name, phone, email, account number or city.

        `amount_band` rather than the amount: the model does not need the rupee figure to
        pick an action, and withholding it makes emitting one structurally impossible.
        """
        return {
            "cause_posterior": {c.value: round(p, 3)
                                for c, p in sorted(posterior.items(),
                                                   key=lambda kv: -kv[1]) if p >= 0.01},
            "days_left": plan_state.days_left,
            "attempts_left": plan_state.attempts_left,
            "contacts_left": plan_state.contacts_left,
            "notice_pending": plan_state.notice_pending,
            "estimated_inflow_in_days": plan_state.days_to_inflow,
            "inflow_confidence": round(inflow.concentration, 2),
            "inflow_observations": inflow.n_observations,
            "mandates": [{"rail": m.rail.value, "status": m.status.value,
                          "cap_paise": m.cap_paise} for m in mandates],
            "alt_rail_available": plan_state.alt_rail,
            "ptp_open": False,
            "hardship_score": round(hardship_score, 2),
            "merchant_category": merchant_category,
            "amount_band": amount_band(amount_paise),
            "eligible": sorted(a.value for a in eligible),
        }

    # ---- the call --------------------------------------------------------------

    def propose(self, plan_state, posterior, eligible, **payload_kw) -> Proposal | None:
        """Returns None on anything unexpected. None is normal and safe: the runner uses
        the planner's choice, which is what it would have done anyway."""
        payload = self.user_payload(plan_state, posterior, eligible, **payload_kw)
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if key in self._cache:
            self.cache_hits += 1
            cached = self._cache[key]
            return Proposal(**{**cached.__dict__, "cached": True})

        body = {
            "model": self.model,
            "temperature": 0.0,
            # Measured against the live API rather than guessed: at 300 the 120b came
            # back empty, at 400 with low effort the completion was 297 tokens.
            "max_tokens": self.max_output_tokens,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(payload)}],
            "response_format": response_format(),
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        if self.exhausted:
            return None

        # What the limiter reserves is prompt + max_tokens, not what the reply costs.
        self._last_reservation = len(json.dumps(body)) // 4 + int(body["max_tokens"])

        for attempt in range(self.max_retries + 1):
            self._throttle()
            started = time.monotonic()
            try:
                self.calls += 1
                raw = self.transport(f"{self.base_url}/chat/completions", body,
                                     self.api_key or "", self.timeout_s)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    OSError) as exc:
                self.failures += 1
                kind = (f"http_{exc.code}" if isinstance(exc, urllib.error.HTTPError)
                        else type(exc).__name__)
                if isinstance(exc, urllib.error.HTTPError):
                    # A 429 states the budget too. Learning only from successes means a
                    # run that starts rate-limited never learns, and re-fires into the
                    # same wall at the floor interval for the whole batch.
                    self._remember_limits(exc.headers)
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    detail = self._read_error(exc)
                    if any(m in detail for m in DAILY_LIMIT_MARKERS):
                        self.exhausted = detail[:200]
                        kind = "http_429_daily_limit"
                        self.failure_kinds[kind] = self.failure_kinds.get(kind, 0) + 1
                        return None
                self.failure_kinds[kind] = self.failure_kinds.get(kind, 0) + 1
                if attempt >= self.max_retries:
                    return None           # never loop; fall through to the planner
                # Honour the server's own backoff before the single retry. Still one
                # retry — waiting when told to is not looping.
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    time.sleep(min(30.0, float(exc.headers.get("retry-after", 5) or 5)))
                continue

            latency = int((time.monotonic() - started) * 1000)
            self.latencies_ms.append(latency)
            limits = raw.get("_ratelimit") or {}
            self._remaining_tokens = (int(limits["remaining_tokens"])
                                      if limits.get("remaining_tokens") is not None
                                      else None)
            self._reset_tokens_s = _reset_seconds(limits.get("reset_tokens"))
            try:
                content = raw["choices"][0]["message"]["content"]
                proposal = parse(json.loads(content), self.name, latency)
            except (KeyError, IndexError, ValueError, TypeError):
                self.invalid += 1
                return None

            if proposal.action_type not in eligible:
                # PROPOSER_INVALID: the model chose something outside the set it was
                # given. Discard it and let the planner decide — do not re-prompt.
                self.invalid += 1
                return None

            self._cache[key] = proposal
            return proposal
        return None

    def _throttle(self) -> None:
        """`min_interval_s` is the floor; the server's own figures are the authority.

        If the remaining per-minute token budget will not cover what this call reserves,
        wait for the stated reset instead of firing into a 429 and calling the fallback
        a measurement.
        """
        elapsed = time.monotonic() - self._last_call_at
        wait = (self.min_interval_s - elapsed) if self._last_call_at else 0.0
        if (self._remaining_tokens is not None and self._reset_tokens_s is not None
                and self._remaining_tokens < self._last_reservation):
            wait = max(wait, self._reset_tokens_s + 0.25)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _remember_limits(self, headers: Any) -> None:
        remaining = headers.get("x-ratelimit-remaining-tokens")
        if remaining is not None:
            self._remaining_tokens = int(remaining)
        self._reset_tokens_s = (_reset_seconds(headers.get("x-ratelimit-reset-tokens"))
                                or _reset_seconds(headers.get("retry-after")))

    @staticmethod
    def _read_error(exc: urllib.error.HTTPError) -> str:
        try:
            return json.loads(exc.read()).get("error", {}).get("message", "").lower()
        except Exception:
            return ""

    def stats(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        pick = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else 0
        return {"proposer": self.name, "calls": self.calls,
                "cache_hits": self.cache_hits, "invalid": self.invalid,
                "failures": self.failures, "failure_kinds": dict(self.failure_kinds),
                "exhausted": self.exhausted,
                "latency_p50_ms": pick(0.50), "latency_p95_ms": pick(0.95)}
