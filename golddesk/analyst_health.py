"""Is the analyst still answering, and still answering WELL?

WHY THIS IS ITS OWN AXIS

self_audit asks "is the desk wired". capture asks "is it still exploiting".
Neither can see the analyst degrade, and it can degrade in ways that look
exactly like a quiet market:

  it stops answering at all -- a wedged CLI session, an expired login, a
  provider outage. Observed 2026-08-27: dozens of reads discarded on prose
  length, and the ledger recorded nothing at all for them.

  it keeps answering but SLOWER, drifting toward the timeout. Every read that
  crosses it is silently lost, and the first symptom is fewer decisions --
  which reads as selectivity.

  it quietly answers as a DIFFERENT MODEL or at a lower effort. A fallback is
  supposed to be visible; a fallback nobody notices is a permanent downgrade.
  Observed on the quant desk: "roster capabilities ABSENT -> EVERY seat runs on
  the 'high' fallback, not its advertised max."

NONE of these raises. All of them look like a desk being careful.

WHAT THIS CANNOT DO, said plainly because it is the limit that matters: it
cannot tell whether a read is CORRECT. That needs resolved trades, and no
number of checks substitutes for them. It measures whether the analyst is
RESPONDING, how fast, and as what -- the mechanical half, which is the half that
fails silently.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

ANALYST_HEALTH_VERSION = "analyst-2026-08-28-a"

#: Wakes in a window before an answer RATE means anything.
MIN_WAKES = 20

#: Fraction of recent wakes the analyst may fail to answer before it is a fault
#: rather than a blip. A fifth is generous on purpose: a busy provider drops the
#: occasional call, and a check that fires on one timeout is one nobody reads.
BLIND_FRACTION = 0.20

#: Reads before a LATENCY comparison means anything.
MIN_READS = 10

#: Fraction of the timeout budget the median read may reach before it is worth
#: naming. At 0.75 of the budget, ordinary variance is already crossing it and
#: those reads are being lost silently.
LATENCY_WARN_FRAC = 0.75

#: The desk's primary read budget, seconds. Mirrors providers.DEFAULT_TIMEOUT_S;
#: passed in by the caller where it matters, defaulted here so the module has no
#: import-time dependency on the provider layer.
DEFAULT_BUDGET_S = 600.0


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'BROKEN'}] {self.check:<22} {self.detail}"


def _ts(r: dict) -> Optional[datetime]:
    raw = r.get("t0") or r.get("ts")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _recent(rows: Sequence[dict], now: datetime, hours: float) -> list[dict]:
    cut = now - timedelta(hours=hours)
    return [r for r in rows if (_ts(r) or now) >= cut]


def _is_degraded(row: dict) -> bool:
    """Was this decision served by the rule-based fallback rather than a model?

    Read off the stamp the desk writes (usage.degraded) rather than inferred
    from the provider name, so a future fallback of any kind is covered by the
    same flag instead of needing a new name added here.
    """
    dec = row.get("decision") or {}
    if (dec.get("usage") or {}).get("degraded"):
        return True
    return bool(dec.get("degraded"))


def _stamps(rows: Sequence[dict]) -> list[dict]:
    """Decision rows carrying a provider stamp -- the analyst actually ran."""
    out = []
    for r in rows:
        dec = r.get("decision") or {}
        if dec.get("provider") or dec.get("model"):
            out.append(dec)
    return out


def check_answer_rate(rows: Sequence[dict], now: Optional[datetime] = None,
                      hours: float = 12.0) -> Finding:
    """How often a wake produced an answer rather than a BLIND row.

    This is the check that would have caught 2026-08-27 in the hour it started
    instead of at the end of the day.
    """
    now = now or datetime.now(timezone.utc)
    rec = _recent(rows, now, hours)
    answered = [r for r in rec if str(r.get("kind", "")) in
                ("SIGNAL", "REFUSAL_MODEL", "REFUSAL_COMPILER", "REFUSAL_ROUTER")
                and not _is_degraded(r)]
    # A DEGRADED ROW IS NOT AN ANSWER. When the analyst is unreachable the desk
    # falls back to its rule-based reader, which produces SIGNAL and REFUSAL
    # rows like any other -- so without this exclusion the fallback would MASK
    # the very outage it exists to survive, and this check would report a
    # perfectly healthy analyst while nothing had reached one for hours. That is
    # WS-005 with extra steps: a mechanism that makes absence look like an
    # answer.
    degraded = [r for r in rec if _is_degraded(r)]
    blind = [r for r in rec if r.get("kind") == "BLIND"]
    total = len(answered) + len(blind) + len(degraded)
    if degraded and total >= MIN_WAKES:
        frac = (len(blind) + len(degraded)) / total
        return Finding("analyst answering", False,
                       f"{len(degraded)} of {total} wakes in {hours:.0f}h were "
                       f"served by the RULE-BASED FALLBACK and {len(blind)} got "
                       f"nothing — {frac:.0%} of decisions did not come from the "
                       f"analyst. The desk is still producing signals, which is "
                       f"why this does not look like an outage anywhere else.")
    if total < MIN_WAKES:
        return Finding("analyst answering", True,
                       f"UNMEASURED — {total} wake(s) in {hours:.0f}h, under {MIN_WAKES}")
    frac = len(blind) / total
    if frac > BLIND_FRACTION:
        # IS IT STILL HAPPENING? This is a RATE over a window, which is the
        # right shape for the question it asks -- but a rate cannot tell an
        # ongoing outage from a resolved one, and for twelve hours after a fix
        # it reads exactly like the outage. Naming the current state costs one
        # sentence and stops the report contradicting the desk's own ANALYST
        # BACK message, which is how a whole report gets distrusted.
        # TIES BREAK BY LEDGER ORDER, not by whichever row max() happened to
        # see first. Rows written in the same second are ordinary -- a wake
        # produces its decision immediately -- and a bare max() on timestamp
        # picked an ANSWERED row out of a batch that ended in BLIND, reporting a
        # desk blind on 25 of 25 wakes as recovered. The ledger is append
        # ordered, so position is the tiebreaker that means anything.
        latest = (max(enumerate(rec), key=lambda p: ((_ts(p[1]) or now), p[0]))[1]
                  if rec else None)
        recovered = (latest is not None and latest.get("kind") != "BLIND"
                     and not _is_degraded(latest))
        # ok=recovered, not `not recovered`. Inverted on the first attempt,
        # which reported a desk blind on 25 of 25 wakes as PASSING.
        return Finding("analyst answering", recovered,
                       f"{len(blind)}/{total} wakes ({frac:.0%}) got NO answer in "
                       f"the last {hours:.0f}h. "
                       + ("The MOST RECENT wake answered, so this is a rate over "
                          "a window containing a RESOLVED outage — not a desk "
                          "that is blind now."
                          if recovered else
                          "The desk is BLIND on those bars, not selective — a "
                          "wedged session, an expired login or a provider outage "
                          "all look like this."))
    return Finding("analyst answering", True,
                   f"{len(answered)}/{total} wakes answered ({1 - frac:.0%})")


def check_latency(rows: Sequence[dict], now: Optional[datetime] = None,
                  hours: float = 12.0,
                  budget_s: float = DEFAULT_BUDGET_S) -> Finding:
    """Median read latency against the timeout budget.

    Drift toward the budget is the failure mode nobody sees: every read that
    crosses it is lost, the desk simply decides less, and less deciding reads as
    discipline. Median rather than mean -- one 600s outlier should not define
    the reading, and the question is where the BULK of reads sit.
    """
    now = now or datetime.now(timezone.utc)
    lat = [float(d["latency_ms"]) / 1000.0
           for d in _stamps(_recent(rows, now, hours))
           if d.get("latency_ms") is not None]
    if len(lat) < MIN_READS:
        return Finding("analyst latency", True,
                       f"UNMEASURED — {len(lat)} stamped read(s), under {MIN_READS}")
    med = statistics.median(lat)
    frac = med / budget_s if budget_s else 0.0
    if frac > LATENCY_WARN_FRAC:
        return Finding("analyst latency", False,
                       f"median read {med:.0f}s against a {budget_s:.0f}s budget "
                       f"({frac:.0%}). Ordinary variance is already crossing it, "
                       f"and every read that does is lost silently — the desk "
                       f"just decides less, which looks like selectivity.")
    return Finding("analyst latency", True,
                   f"median {med:.0f}s of {budget_s:.0f}s budget ({frac:.0%}) "
                   f"over {len(lat)} reads")


def check_model_is_what_was_asked_for(rows: Sequence[dict],
                                      expected_model: Optional[str] = None,
                                      now: Optional[datetime] = None,
                                      hours: float = 12.0) -> Finding:
    """Is the analyst answering as the model the desk was configured with?

    A fallback is supposed to be visible. A fallback nobody notices is a
    permanent downgrade wearing the configured name -- exactly the shape of the
    quant desk's "roster capabilities ABSENT -> every seat runs on the 'high'
    fallback, not its advertised max".
    """
    now = now or datetime.now(timezone.utc)
    models = [str(d.get("model")) for d in _stamps(_recent(rows, now, hours))
              if d.get("model")]
    if not models:
        return Finding("analyst model", True, "no stamped reads in the window")
    seen = sorted(set(models))
    if expected_model and any(expected_model not in m for m in seen):
        odd = [m for m in seen if expected_model not in m]
        return Finding("analyst model", False,
                       f"reads answered as {odd} while the desk was configured "
                       f"for {expected_model!r}. A fallback nobody notices is a "
                       f"permanent downgrade.")
    if len(seen) > 1:
        return Finding("analyst model", False,
                       f"{len(seen)} different models answered in {hours:.0f}h "
                       f"({seen}) — reads from different models are not "
                       f"comparable evidence and land in the same cohort.")
    return Finding("analyst model", True, f"all reads answered as {seen[0]}")


#: The CLI's own words for an expired subscription login, lowercased. Kept here
#: rather than imported from providers so this module stays free of an
#: import-time dependency on the provider layer, matching DEFAULT_BUDGET_S's
#: existing convention; test_flag_ladder pins the two lists together.
LOGIN_MARKERS = ("failed to authenticate", "oauth session expired",
                 "please run /login", "invalid api key")


def check_login(rows: Sequence[dict], now: Optional[datetime] = None,
                hours: float = 12.0) -> Finding:
    """Has the CLI's login expired?

    A SEPARATE CHECK BECAUSE IT NEEDS NO SAMPLE. Every other check here is
    statistical and refuses to speak under MIN_WAKES, which is right: one
    timeout means nothing. But an expired login is not a rate to estimate, it is
    a fact the CLI states outright, and one row carrying it is conclusive.

    It also matters that this cannot self-clear. check_answer_rate says "the
    desk is blind, and here are the three things it might be"; this says which
    one, and what clears it. Observed 2026-08-28: every read from ~21:00
    returned `{"subtype":"success", "api_error_status":null, "result":"Failed to
    authenticate: OAuth session expired and could not be refreshed"}` -- an
    envelope that says "success" twice over while naming the failure once, in
    the one field nothing was reading.
    """
    now = now or datetime.now(timezone.utc)
    rec = _recent(rows, now, hours)
    hits = []
    for r in rec:
        if r.get("kind") != "BLIND":
            continue
        dec = r.get("decision") or {}
        blob = " ".join(str(x) for x in
                        (dec.get("cli") or {}).values() if x is not None).lower()
        blob += " " + str(dec.get("error") or "").lower()
        if dec.get("needs_login") or (dec.get("cli") or {}).get("needs_login") \
                or any(m in blob for m in LOGIN_MARKERS):
            hits.append(r)
    if not hits:
        return Finding("analyst login", True,
                       f"no login failure in {hours:.0f}h")

    # HAS IT BEEN FIXED SINCE? A window check fires on any matching row in the
    # last 12 hours, so it stayed BROKEN for twelve hours AFTER a successful
    # login -- and on 2026-08-28 it did exactly that: Telegram said ANALYST BACK
    # while this said THE LOGIN HAS EXPIRED, at the same moment, both from the
    # same ledger. A contradiction like that does not get investigated, it gets
    # the whole report distrusted.
    #
    # Same lesson as check_excursion_survives, one file over: a check that
    # cannot clear when the thing it names is fixed is not a check. The
    # discriminator is ORDER -- a real analyst answer AFTER the last login
    # failure means the credential works now, whatever happened this morning.
    last_fail = _ts(hits[-1]) or now
    answered_after = [
        r for r in rec
        if str(r.get("kind", "")) in ("SIGNAL", "REFUSAL_MODEL",
                                      "REFUSAL_COMPILER", "REFUSAL_ROUTER")
        and not _is_degraded(r) and (_ts(r) or now) > last_fail]
    if answered_after:
        back = _ts(answered_after[0])
        return Finding("analyst login", True,
                       f"RECOVERED — {len(hits)} login failure(s) earlier in the "
                       f"window, but the analyst has answered "
                       f"{len(answered_after)} time(s) since the last one"
                       + (f" (first at {back:%H:%M}Z)" if back else "")
                       + ". The credential works now.")

    first = _ts(hits[0]) or now
    return Finding("analyst login", False,
                   f"THE LOGIN HAS EXPIRED — {len(hits)} blind wake(s) since "
                   f"{first:%Y-%m-%d %H:%M}Z carry the CLI's own "
                   f"'OAuth session expired' message, and NOTHING has answered "
                   f"since. No retry, restart, flag change or watchdog clears "
                   f"this: run `claude setup-token` on the box, as the user the "
                   f"scheduled task runs as, and complete the browser login. "
                   f"Reads resume on the next wake.")


#: The CLI's words for an exhausted subscription quota. Kept beside
#: LOGIN_MARKERS for the same reason: this module must not import the provider
#: layer, and a test pins both lists against it.
QUOTA_MARKERS = ("session limit", "usage limit", "quota")


def check_quota(rows: Sequence[dict], now: Optional[datetime] = None,
                hours: float = 12.0) -> Finding:
    """Is the desk blind because it ran out of subscription quota?

    A DIFFERENT FAULT FROM EVERY OTHER ONE HERE, and it needs saying because
    the remedy is the opposite of the usual. Nothing is broken, nothing needs
    fixing, and every retry makes it worse -- the limit resets on a clock and
    reads resume by themselves. Reported so the operator is not sent to
    re-login, restart or reinstall anything at all.

    Observed live 2026-08-28: "You've hit your session limit - resets 8:10pm
    (Europe/Berlin)", arriving with the same exit 1 / zero tokens / zero API
    time signature as a rejected flag and an expired login.
    """
    now = now or datetime.now(timezone.utc)
    rec = _recent(rows, now, hours)
    hits, latest = [], None
    for r in rec:
        if r.get("kind") != "BLIND":
            continue
        dec = r.get("decision") or {}
        cli = dec.get("cli") or {}
        blob = (" ".join(str(v) for v in cli.values() if v is not None)
                + " " + str(dec.get("error") or "")).lower()
        if dec.get("quota_exhausted") or cli.get("quota_exhausted") \
                or any(m in blob for m in QUOTA_MARKERS):
            hits.append(r)
            latest = str(cli.get("result") or "")[:120] or latest
    if not hits:
        return Finding("analyst quota", True, f"no quota refusal in {hours:.0f}h")

    answered_after = [
        r for r in rec
        if str(r.get("kind", "")) in ("SIGNAL", "REFUSAL_MODEL",
                                      "REFUSAL_COMPILER", "REFUSAL_ROUTER")
        and not _is_degraded(r)
        and (_ts(r) or now) > (_ts(hits[-1]) or now)]
    if answered_after:
        return Finding("analyst quota", True,
                       f"RECOVERED — {len(hits)} quota refusal(s) earlier in the "
                       f"window and the analyst has answered since.")
    return Finding("analyst quota", False,
                   f"SUBSCRIPTION QUOTA EXHAUSTED — {len(hits)} wake(s) refused. "
                   f"{latest or ''} "
                   f"NOTHING IS BROKEN: this is not a login, a flag or an "
                   f"outage, no restart or re-login helps, and every retry "
                   f"spends against a limit that is already gone. Reads resume "
                   f"by themselves at the reset time; until then the desk runs "
                   f"on the rule-based arm.")


def audit(rows: Sequence[dict], now: Optional[datetime] = None,
          expected_model: Optional[str] = None,
          budget_s: float = DEFAULT_BUDGET_S) -> list[Finding]:
    return [
        check_quota(rows, now),
        check_login(rows, now),
        check_answer_rate(rows, now),
        check_latency(rows, now, budget_s=budget_s),
        check_model_is_what_was_asked_for(rows, expected_model, now),
    ]


def render(findings: Sequence[Finding]) -> str:
    bad = [f for f in findings if not f.ok]
    head = (f"ANALYST HEALTH ({ANALYST_HEALTH_VERSION}) — "
            + ("responding normally" if not bad else f"{len(bad)} FAULT(S)"))
    out = [head] + [f.line for f in findings]
    if bad:
        out += ["",
                "  These measure whether the analyst is RESPONDING, how fast, and",
                "  as what. None of them can tell whether a read is CORRECT --",
                "  that needs resolved trades, and no number of checks is a",
                "  substitute for them."]
    return "\n".join(out)
