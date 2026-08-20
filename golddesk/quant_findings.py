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

BOTH ARE NOW SEALED; THE SCHEMA GAP THAT BLOCKED THE SECOND IS CLOSED

Sealing a QUEUED finding into a testable golddesk.hypothesis.Hypothesis requires
a `selector` — a cohort match against Aurum's OWN ledger rows, checked by
`Hypothesis.matches(ctx, decision)` against the row's `context` dict and the row
itself. hunt14's XAUUSD survivor is conditioned on the PRIOR NIGHT's NY-session
displacement quality (state in {NORMAL_DAY, TREND_DAY, RANGE_DAY,
FAILED_BREAK}). That state previously did not exist anywhere in Aurum; it is
now `golddesk.day_state.read()`, a direct port of quant's own classifier
(run_hunt12.day_states(), the version actually used by run_hunt14.py — see
that module's docstring for the exact thresholds and a discrepancy this port
deliberately resolved one way, named there rather than silently picked),
wired into `MarketBrief.day_state` by `runner.build_brief` and attached to
every ledger row's context as `prior_ny_session_state` by
`golddesk.live.LiveDesk._trend_ctx`. Writing a selector against a key that
never appears in a ledger row would not test the finding; it would silently
never match anything, forever, which is worse than not sealing it, because it
LOOKS wired and is dead. That trap no longer applies here: the key is real
and populated on every live decision from this commit forward.

Both findings start at post_n=0 regardless — sealing only means the
hypothesis can now accrue real evidence going forward, not that it already
has any. `reports/hunt12.json` / `reports/hunt14.json` are gitignored in the
quant repository and were not independently re-derivable at port time; only
the classifier code that would produce them was.

RUN THIS to (re-)apply the queue decisions and print the record:

    python3 -m golddesk.quant_findings
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .absorb import Absorbed, Absorber, Finding
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
        "Aurum's own ASIA-session trades, on days whose prior_ny_session_state "
        "(golddesk.day_state.read(), attached to every ledger row's context by "
        "golddesk.live.LiveDesk._trend_ctx) reads NORMAL_DAY, show positive "
        "mean R with post-seal n >= 25, ESS >= 20, 3/4 quarters agreeing, "
        "matching this finding's positive sign."),
    meta={"hunt": "hunt14", "window": "asia", "state": "NORMAL_DAY",
         "n": 760, "expectancy_r": 0.227, "t": 5.79, "deflated_t": 2.87,
         "pf": 1.67},
)

# hunt14 conditions on the ASIA window specifically -- the selector matches
# only the day-level state, so a caller sealing entries against this
# hypothesis is still responsible for checking session=="ASIA" itself, same
# as hunt14's own grid did.
XAUUSD_ASIA_NORMAL_DAY_SELECTOR = {"prior_ny_session_state": "NORMAL_DAY"}


def _seal_if_new(ab: Absorber, book: HypothesisBook, finding: Finding,
                 hid: str, selector: dict, predicted_sign: int) -> Absorbed:
    """Queue one finding and seal it into `book` if it isn't already sealed.

    Idempotent on both the absorber's content hash and the hypothesis book's
    selector, so re-running `apply()` (the module's own __main__ entry point)
    never creates a duplicate hypothesis or re-seals one already accruing.
    """
    a = ab.queue(finding)
    if a.status != "QUEUED":
        return a
    existing = next((h for h in book.items.values() if h.selector == selector), None)
    if existing is not None:
        return a
    now = datetime.now(timezone.utc).isoformat()
    h = Hypothesis(
        hid=hid, statement=finding.statement, selector=selector,
        predicted_sign=predicted_sign,
        discovered_on=now[:10], seal_ts=now,
        discovery_n=0, discovery_mean_r=0.0)
    book.seal(h)
    return ab.seal(finding, h.hid)


def apply(secrets_dir: Path = Path(".")) -> Absorber:
    """Queue both findings, seal both, leave the record on disk."""
    ab = Absorber.load(FINDINGS_LOG) if FINDINGS_LOG.exists() else Absorber()
    book = HypothesisBook(HYPOTHESIS_BOOK_PATH)

    _seal_if_new(ab, book, TREND_STRENGTH_FINDING,
                "quant-trend-strength-high-v1", TREND_STRENGTH_SELECTOR, 1)
    _seal_if_new(ab, book, XAUUSD_ASIA_NORMAL_DAY_FINDING,
                "quant-xauusd-asia-normal-day-v1",
                XAUUSD_ASIA_NORMAL_DAY_SELECTOR, 1)

    ab.save(FINDINGS_LOG)
    log.info("quant findings applied: %s", ab.report())
    return ab


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = apply()
    print(result.report())
