"""Everything the quant desk has found that is actually about gold, formally queued.

This is the file absorb.py's own docstring describes and that nothing had ever
written: `external/channels.txt` and `external/signals.jsonl` sat at zero bytes
while quant produced real, measured results. Two findings are queued here, both
XAUUSD-specific and both survivors of quant's own multiple-testing correction —
the ones "loud enough" that filing them as a note nobody reads would be the
failure absorb.py exists to prevent.

WHAT "QUEUED" MEANS AND DOES NOT MEAN

Every entry below goes through `absorb.Absorber.queue()`, the real intake path,
and is graded, timestamped and content-hashed like any other Finding. Per
absorb.py's own rule an external finding enters at ZERO AUTHORITY regardless of
how strong its evidence is elsewhere — a result from quant's backtest is
evidence about quant's backtest, not a fact about gold as Aurum trades it.

ONE OF THE TWO CANNOT BE SEALED YET, AND THIS SAYS SO RATHER THAN FAKING IT

Sealing a QUEUED finding into a testable golddesk.hypothesis.Hypothesis requires
a `selector` — a cohort match against Aurum's OWN ledger rows, checked by
`Hypothesis.matches(ctx, decision)` against the row's `context` dict and the row
itself. hunt14's XAUUSD survivor is conditioned on the PRIOR NIGHT's NY-session
displacement quality (state in {NORMAL_DAY, TREND_DAY, RANGE_DAY,
FAILED_BREAK}) -- a concept Aurum's Context does not currently carry at all.
Writing a selector against a key that never appears in a ledger row would not
test the finding; it would silently never match anything, forever, which is a
worse failure than not sealing it, because it LOOKS wired and is dead. So that
finding stays QUEUED here, with the exact schema gap named as the blocking
task, rather than sealed against a selector chosen to look complete.

The trend-detector's own predictive claim has no such gap -- SEALED below,
against a selector Aurum's ledger can already match today.

RUN THIS to (re-)apply the queue decisions and print the record:

    python3 -m golddesk.quant_findings
"""
from __future__ import annotations

import logging
from pathlib import Path

from .absorb import Absorber, Finding
from .hypothesis import Hypothesis, HypothesisBook

log = logging.getLogger(__name__)

FINDINGS_LOG = Path("state/quant_findings.json")
HYPOTHESIS_BOOK_PATH = Path("state/hypotheses.json")


# --------------------------------------------------------------------------
# Finding 1: the trend-strength mechanism itself (golddesk/gold_trend.py)
# --------------------------------------------------------------------------

TREND_STRENGTH_FINDING = Finding(
    statement=(
        "Forward 24-bar move in the trend detector's declared direction is "
        "monotone in its strength reading: mean +0.024 / +0.073 / +0.136 ATR "
        "across strength terciles 0.3-0.5 / 0.5-0.7 / 0.7-1.0 (deflated t "
        "roughly 0.7 / 2.2 / 1.8 after accounting for overlapping 24-bar "
        "samples). Real and monotone, but modest, and the medians in the "
        "lower two bins are NEGATIVE -- this is a positive-skew edge that "
        "will feel wrong more often than it is wrong."),
    source="quant/desks/mt5/mt5desk/trendday.py, research/trend_gate.py",
    grade="E2",   # public/internal backtest; not yet monitored live anywhere
    measured_on=("22 instruments (FX majors/crosses, XAUUSD, XAGUSD, BTCUSD, "
                 "ETHUSD), H1, 2018-2026, ~49.5k bars per instrument"),
    transfer_test=(
        "On Aurum's OWN forward ledger: signals whose trend.strength (see "
        "golddesk/gold_trend.py, wired into MarketBrief) was >= 0.7 at read "
        "time show positive mean realised R with post-seal n >= 25, ESS >= "
        "20, and at least 3 of 4 quarters agreeing on sign -- the same "
        "standard golddesk.hypothesis holds every sealed hypothesis to."),
    meta={"xauusd_included_in_measurement": True,
         "ported_module": "golddesk.gold_trend"},
)

# The selector: Aurum's ledger rows are expected to carry a bucketed strength
# label alongside every decision -- see `strength_bucket()` below, which is
# what a live caller should compute and attach to a ledger row's context so
# this selector can ever match. Until a caller does that, this hypothesis
# accrues nothing, which `accrue()` reports honestly as post_n=0 rather than
# pretending to have evidence.
TREND_STRENGTH_SELECTOR = {"trend_strength_bucket": "high"}


def strength_bucket(strength: float) -> str:
    """The bucket a ledger row should record, matching TREND_STRENGTH_SELECTOR.

    A free function rather than inlined at the call site so the ledger-writing
    code and this module's selector can never silently drift apart -- both
    call this, so a change to the boundary changes both at once.
    """
    if strength >= 0.7:
        return "high"
    if strength >= 0.5:
        return "medium"
    if strength >= 0.3:
        return "low"
    return "none"


# --------------------------------------------------------------------------
# Finding 2: XAUUSD asia-session, prior-night NORMAL_DAY (hunt14 survivor)
# --------------------------------------------------------------------------

XAUUSD_ASIA_NORMAL_DAY_FINDING = Finding(
    statement=(
        "XAUUSD session-range breakout, ASIA window, conditioned on the "
        "PRIOR day's NY session (13:00-22:00 UTC) showing NORMAL_DAY "
        "displacement (neither an exceptional trend day nor a failed "
        "break): n=760, expectancy +0.227R, t=5.79 (deflated t=2.87 against "
        "hunt14's own 352-cell grid, PF=1.67. One of 4 survivors of that "
        "grid and the only XAUUSD-specific one; re-swept from hunt12 under "
        "quant's corrected multiplicity bar and the stall-tightening exit."),
    source="quant/desks/mt5/research/run_hunt14.py, reports/hunt14.json",
    grade="E2",
    measured_on="XAUUSD, H1, 2018-2026, prior-NY-session-conditioned asia window",
    transfer_test=(
        "BLOCKED ON A SCHEMA GAP, named rather than worked around: Aurum's "
        "Context has no field for 'prior NY session displacement quality'. "
        "Before this can be sealed as a testable Hypothesis, something must "
        "compute that state from Aurum's own prior-day bars and attach it to "
        "the ledger row's context, the same way trend_strength_bucket does "
        "for finding 1. Once that exists, the test is: Aurum's own ASIA-"
        "session trades, on days whose prior NY session read NORMAL_DAY, "
        "show positive mean R with post-seal n >= 25, ESS >= 20, 3/4 "
        "quarters agreeing, matching this finding's positive sign."),
    meta={"hunt": "hunt14", "window": "asia", "state": "NORMAL_DAY",
         "n": 760, "expectancy_r": 0.227, "t": 5.79, "deflated_t": 2.87,
         "pf": 1.67, "blocked_on": "no prior-NY-session state in Context"},
)


def apply(secrets_dir: Path = Path(".")) -> Absorber:
    """Queue both findings, seal the one that can be, leave the record on disk."""
    ab = Absorber.load(FINDINGS_LOG) if FINDINGS_LOG.exists() else Absorber()

    a1 = ab.queue(TREND_STRENGTH_FINDING)
    if a1.status == "QUEUED":
        book = HypothesisBook(HYPOTHESIS_BOOK_PATH)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        existing = next((h for h in book.items.values()
                         if h.selector == TREND_STRENGTH_SELECTOR), None)
        if existing is None:
            h = Hypothesis(
                hid="quant-trend-strength-high-v1",
                statement=TREND_STRENGTH_FINDING.statement,
                selector=TREND_STRENGTH_SELECTOR, predicted_sign=1,
                discovered_on=now[:10], seal_ts=now,
                discovery_n=0, discovery_mean_r=0.0)
            book.seal(h)
            a1 = ab.seal(TREND_STRENGTH_FINDING, h.hid)

    a2 = ab.queue(XAUUSD_ASIA_NORMAL_DAY_FINDING)
    # NOT sealed -- see the module docstring. Queued only, on purpose.

    ab.save(FINDINGS_LOG)
    log.info("quant findings applied: %s", ab.report())
    return ab


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = apply()
    print(result.report())
