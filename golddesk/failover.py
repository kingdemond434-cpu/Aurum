"""When the first brain is unavailable, ask the second one. Never invent a third.

THE OBSERVED FAILURE. The desk's analyst goes dark for reasons that have nothing
to do with gold: a subscription session limit that resets at 8:10pm, an expired
OAuth login, a provider outage, a timeout. Every one of those produces the same
envelope — exit 1, zero tokens, zero duration — and the desk's honest answer was
to record BLIND and send nothing. Correct when there is no brain available.
Wrong when a perfectly good one is installed on the same machine and nobody
asked it.

WHAT THIS DOES. An ordered chain of analyst providers, each given the IDENTICAL
frozen brief and the identical schema, tried in order until one answers. The
answer goes through the same deterministic compiler as every other read: the
compiler owns entry, stop, targets, costs, expectancy and risk, and it does not
know which brain produced the thesis. That property is the reason a second brain
is safe to add at all — a fallback analyst cannot loosen a gate, move a stop or
price a trade, because none of those were ever the analyst's to decide.

FOUR RULES, AND THE LAST ONE IS THE IMPORTANT ONE

  SAME EVIDENCE     every provider gets the same brief and the same charts. A
                    provider that cannot take charts REFUSES them rather than
                    reading a subset and letting the ledger record it as the
                    same kind of read. That refusal is a failure, and the chain
                    moves on — which is right: a text-only read is a different
                    experiment, and it will be recorded as one.

  SAME SCHEMA       every provider returns AnalystRead or raises. Nothing here
                    accepts a looser answer from a fallback because it was the
                    last one available.

  ALWAYS STAMPED    the read carries `fallback_from`, `fallback_reason`,
                    `chain_position` and the failures that preceded it. A
                    decision made by a different model must never be
                    indistinguishable in the record from one the primary made,
                    because the whole point of running two brains is being able
                    to measure them separately afterwards.

  NEVER FABRICATE   when the chain is exhausted the chain FAILS. It does not
                    reach for something weaker to keep the message cadence up.
                    Frequency is a standing order of this desk and it is a real
                    one, but it means "do not refuse trades out of timidity" —
                    it has never meant "produce a signal from a brain nobody
                    validated so the hour has a message in it". The desk's
                    existing rule-based degrade still sits downstream in
                    live.py, where it labels itself DEGRADED on every row it
                    touches; this module does not pre-empt it and does not
                    pretend to be it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .analyst import MarketBrief
from .chart import Chart
from .providers import AnalystError, AnalystProvider, ProviderRead

log = logging.getLogger(__name__)

FAILOVER_VERSION = "failover-2026-08-29-a"


@dataclass(frozen=True)
class Attempt:
    """One brain, asked, and what came back. Kept even when it succeeded."""
    provider: str
    model: str
    ok: bool
    reason: str = ""
    ms: float = 0.0

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "ok": self.ok,
                "reason": self.reason[:300], "ms": round(self.ms, 1)}


#: HOW LONG A FAILED BRAIN IS SKIPPED before it is tried again, by failure
#: class. These are deliberately SHORT. The desk switches back to its primary
#: the moment the primary answers, and it can only answer if it is asked, so a
#: long cool-off would leave the desk on its second brain for hours after the
#: first one recovered — which is the failure this whole module exists to avoid,
#: pointed the other way.
#:
#: The cost of a short cool-off is one wasted call per window while an outage
#: lasts. The cost of a long one is every signal in between coming from the
#: wrong brain. The second is much more expensive, so these lean short.
#:
#:   quota    a session limit that resets on a clock nobody here can read; ten
#:            minutes finds the reset quickly without hammering the limit, and
#:            providers.py already notes that quota is the one failure that gets
#:            WORSE when retried immediately.
#:   auth     an operator can re-login at any second, so probe often.
#:   absent   a binary that is not installed will not appear on its own.
#:   timeout  no cool-off at all: a slow call says nothing about the next one.
#:   error    likewise. Unexplained failures are retried.
COOLOFF_SECONDS = {"quota": 600.0, "auth": 300.0, "absent": 1800.0,
                   "timeout": 0.0, "error": 0.0}


def classify(err: Exception) -> str:
    """A short, stable label for WHY a brain was unavailable.

    Deliberately coarse. The desk has learned the hard way that a rejected flag,
    an expired login and an exhausted quota produce byte-identical envelopes, so
    a fine-grained taxonomy built on message text would be confident and wrong.
    These four buckets are what the ledger can actually support, and each one is
    a different operator action.
    """
    s = str(err).lower()
    if "quota" in s or "usage limit" in s or "session limit" in s or "rate limit" in s:
        return "quota"
    if "auth" in s or "login" in s or "credential" in s or "unauthor" in s:
        return "auth"
    if "timed out" in s or "timeout" in s:
        return "timeout"
    if "unavailable" in s or "not on path" in s or "could not start" in s:
        return "absent"
    return "error"


class ChainAnalyst(AnalystProvider):
    """Try each brain in order. Stamp which one answered and why."""

    name = "chain"

    def __init__(self, providers: Sequence[AnalystProvider]):
        if not providers:
            raise ValueError("a failover chain needs at least one provider")
        self.providers = list(providers)
        self.model = ",".join(getattr(p, "model", "?") or "?" for p in self.providers)
        #: Every attempt of the LAST call, for the audit. Not state the desk
        #: reasons with — it is overwritten each call and exists so a watchdog
        #: can see what happened without re-running anything.
        self.last_attempts: list[Attempt] = []
        #: provider name -> monotonic deadline before which it is skipped.
        self._cool: dict[str, float] = {}
        #: Which brain answered last, so a switch in EITHER direction is an
        #: event that gets logged and stamped rather than a silent drift. The
        #: desk went blind, fell back, recovered and nobody could tell from the
        #: ledger when the primary came back — this is what fixes that.
        self._last_answered: Optional[int] = None
        self._degraded_since: Optional[float] = None
        #: Seconds spent on a fallback, set on the read that recovers and
        #: consumed once by the stamp, so ONE row carries the return rather than
        #: every row after it repeating it.
        self._recovered_after: Optional[float] = None
        #: Injectable so cool-off behaviour is testable without sleeping. A test
        #: that waits ten real minutes is a test nobody runs.
        self.clock = time.monotonic

    # -------------------------------------------------------------- plumbing

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model,
                "version": FAILOVER_VERSION,
                "chain": [p.describe() for p in self.providers]}

    def _note_switch(self, i: int) -> None:
        """Log a change of brain in EITHER direction, and time the degradation.

        Falling back is loud already. COMING BACK was not, and that is the half
        that mattered: the desk could spend an afternoon on its second brain,
        recover, and leave nothing in the record saying when — so "which brain
        produced this week's signals" was unanswerable even though both brains
        were stamped on every row.
        """
        prev = self._last_answered
        self._last_answered = i
        if prev is None or prev == i:
            if i > 0 and self._degraded_since is None:
                self._degraded_since = self.clock()
            return
        if i == 0:
            secs = (self.clock() - self._degraded_since) if self._degraded_since else 0.0
            self._recovered_after = secs
            self._degraded_since = None
            log.warning("PRIMARY ANALYST RECOVERED — back on %s after %.0fs on a "
                        "fallback brain", self.providers[0].name, secs)
        else:
            self._degraded_since = self._degraded_since or self.clock()
            log.warning("ANALYST FAILOVER — now reading with %s (position %d)",
                        self.providers[i].name, i)

    def _stamped(self, pr: ProviderRead, i: int, attempts: list[Attempt]
                 ) -> ProviderRead:
        if i == 0 and self._recovered_after is not None:
            # THE ROW THAT MARKS THE RETURN. Stamped once, on the first read the
            # primary answers after a fallback spell, so the ledger says when the
            # desk came back as well as when it left.
            import dataclasses
            secs, self._recovered_after = self._recovered_after, None
            return dataclasses.replace(pr, failover={
                "version": FAILOVER_VERSION, "chain_position": 0,
                "recovered": True, "degraded_seconds": round(secs, 1),
                "attempts": [a.to_dict() for a in attempts]})
        if i == 0:
            # THE PRIMARY ANSWERED. Nothing is stamped, so an ordinary read's
            # record is byte-identical to what it was before this module
            # existed, and `failover` in the ledger means what it says.
            return pr
        failed = [a for a in attempts if not a.ok]
        import dataclasses
        return dataclasses.replace(pr, failover={
            "version": FAILOVER_VERSION,
            "chain_position": i,
            "fallback_from": failed[0].provider if failed else "unknown",
            "fallback_reason": failed[0].reason[:200] if failed else "unknown",
            "fallback_class": classify(RuntimeError(failed[0].reason)) if failed
            else "unknown",
            "attempts": [a.to_dict() for a in attempts],
        })

    def _walk(self, call, *args) -> tuple[Any, int, list[Attempt]]:
        """Ask each brain in turn. Returns (result, index, attempts).

        Only AnalystError moves the chain along. Anything else — a bug in this
        desk's own code, a broken brief, a KeyboardInterrupt — propagates
        untouched: falling over to a second brain because the FIRST one exposed
        a defect here would hide the defect and produce a signal from it.
        """
        attempts: list[Attempt] = []
        now = self.clock()
        for i, p in enumerate(self.providers):
            until = self._cool.get(p.name, 0.0)
            if until > now:
                # SKIPPED, NOT FORGOTTEN. Recorded as an attempt so the ledger
                # shows the chain declined to ask rather than leaving a gap that
                # reads as though this brain was never in the chain.
                attempts.append(Attempt(p.name, getattr(p, "model", "?"), False,
                                        f"cooling off for another "
                                        f"{until - now:.0f}s after a recent "
                                        f"failure", 0.0))
                continue
            t0 = self.clock()
            try:
                out = call(p, *args)
            except AnalystError as e:
                dt = (self.clock() - t0) * 1000
                kind = classify(e)
                cool = COOLOFF_SECONDS.get(kind, 0.0)
                if cool:
                    self._cool[p.name] = self.clock() + cool
                attempts.append(Attempt(p.name, getattr(p, "model", "?"), False,
                                        str(e), dt))
                log.warning("analyst %s unavailable (%s): %s — trying next brain"
                            "%s", p.name, kind, str(e)[:200],
                            f", and skipping it for {cool:.0f}s" if cool else "")
                continue
            # ANSWERED. Clear its cool-off immediately: a brain that just worked
            # is not cooling off, whatever it did ten minutes ago.
            self._cool.pop(p.name, None)
            attempts.append(Attempt(p.name, getattr(p, "model", "?"), True, "",
                                    (self.clock() - t0) * 1000))
            self._note_switch(i)
            return out, i, attempts

        # EXHAUSTED. The chain fails and says exactly what each brain said, so
        # the BLIND row downstream carries the whole story rather than the last
        # error only.
        self.last_attempts = attempts
        raise AnalystError(
            "every analyst in the chain was unavailable: "
            + "; ".join(f"{a.provider}({classify(RuntimeError(a.reason))}): "
                        f"{a.reason[:120]}" for a in attempts)
            + ". Refusing to fabricate a read from an unvalidated brain to keep "
              "the message cadence up.")

    # ------------------------------------------------------------ the contract

    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead:
        pr, i, attempts = self._walk(lambda p: p.read(brief, charts))
        self.last_attempts = attempts
        return self._stamped(pr, i, attempts)

    def survey(self, brief: MarketBrief, charts: Sequence[Chart] = ()):
        out, i, attempts = self._walk(lambda p: p.survey(brief, charts))
        self.last_attempts = attempts
        pr, uni = out
        return self._stamped(pr, i, attempts), uni

    def choose_option(self, system: str, prompt: str,
                      option_ids: Sequence[str]) -> str:
        """Management, through the same chain.

        A provider with no contextual management raises NotImplementedError,
        which is NOT an AnalystError and so does not walk the chain: "this brain
        does not manage positions" is a fact about the configuration, not an
        outage, and treating it as one would silently promote a fallback into a
        role nobody chose for it.
        """
        out, i, attempts = self._walk(
            lambda p: p.choose_option(system, prompt, option_ids))
        self.last_attempts = attempts
        return out


#: The chain `auto` resolves to, in order, keeping only the links that are
#: actually installed on this box. The primary is first and is NEVER dropped:
#: a desk whose primary is missing must fail its preflight loudly, not start
#: quietly on its backup and look healthy while every signal comes from the
#: brain nobody validated.
AUTO_CHAIN = ("claudecode:claude-opus-5", "codexlocal:")


def resolve_auto(**kw) -> tuple[list[str], list[str]]:
    """Which of AUTO_CHAIN are usable here. Returns (kept, skipped-with-reason).

    Probed rather than assumed, because "the fallback is configured" and "the
    fallback can run" are different claims and only the second one helps at 3am.
    A link that cannot answer is left OUT of the chain and named, instead of
    sitting in it as a step that fails on every read.
    """
    from .providers import build_provider
    kept: list[str] = []
    skipped: list[str] = []
    for i, spec in enumerate(AUTO_CHAIN):
        if i == 0:
            kept.append(spec)
            continue
        try:
            p = build_provider(spec, **kw)
        except Exception as e:                                   # noqa: BLE001
            skipped.append(f"{spec}: {type(e).__name__}: {e}")
            continue
        ok, why = (p.available() if hasattr(p, "available") else (True, "assumed"))
        (kept if ok else skipped).append(spec if ok else f"{spec}: {why}")
    return kept, skipped


def build_chain(specs: Sequence[str], **kw) -> ChainAnalyst:
    """Build a chain from provider specs, e.g. ('claudecode:claude-opus-5',
    'codexlocal:').

    Each element is the same spec string `build_provider` already takes, so a
    chain is not a new vocabulary — it is the existing one, in order.
    """
    from .providers import build_provider
    return ChainAnalyst([build_provider(s, **kw) for s in specs if s.strip()])
