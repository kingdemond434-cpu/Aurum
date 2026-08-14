---
title: CFTC positioning data is a three-day-stale snapshot with a six-week hole in late 2025 — using report date instead of publication date is a live leakage vector
source: CFTC Commitments of Traders release schedule and press releases 9138-25, 9147-25
source_type: paper
language: en
evidence_grade: E4
claim: COT data reflects Tuesday positions but is published Friday afternoon, and publication was suspended entirely from 1 October to 12 November 2025 with a backlog cleared only over subsequent weeks, so any Aurum feature that timestamps COT by report date rather than publication date is trained on information that did not exist at decision time.
mechanism: Backtests join positioning data to price by the date the positions were held, because that is the date printed on the report. The desk could not have known those positions until three days later, and during the 2025 suspension could not have known them for six weeks. The resulting look-ahead is small per observation and systematic across the whole sample, which is the profile most likely to survive naive out-of-sample splits and produce a strategy that fails in live trading.
conditions: Applies to any use of COT, including managed-money net positioning, swap-dealer positioning and any derived extreme or z-score.
anti_conditions: Not an issue if the ingestion already stamps rows with actual publication datetime and the backfill respects it; the leakage is also economically negligible if the feature operates at horizons much longer than the three-day lag, which is a measurable question rather than an assumption.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** GOLD-COT-PIT-009
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E4 (official regulator publications)

## OBSERVED FACTS

- COT data reflect positions **as of Tuesday close**. Reporting firms submit Wednesday morning;
  CFTC verifies and publishes **Friday afternoon** (15:30 ET). A **three-day lag** is structural
  and permanent.
- **Publication was interrupted from 1 October to 12 November 2025** due to a lapse in federal
  appropriations (CFTC press release 9138-25). During that period no COT data existed at all.
- The backlog was cleared by publishing at accelerated frequency, with the schedule revised to
  eliminate the backlog by **29 December 2025** (press release 9147-25).

## MY INFERENCE — three distinct problems, commonly collapsed into one

1. **The routine three-day lag.** Well known, and the easy half of the problem.

2. **The backlog replay is the subtle one.** When the CFTC cleared the backlog it published
   *multiple reports in quick succession*. So for the Oct–Dec 2025 window, the relationship
   between report date and publication date is **not a constant three-day offset** — it is
   irregular and report-specific. Any ingestion that hardcodes "publication = report date + 3
   days" is wrong across that entire window, and wrong in a way that *looks* correct because the
   arithmetic runs without error. A constant-offset correction is not sufficient; the actual
   publication datetime must be stored per row.

3. **Absence must be representable.** For six weeks there was no data. A pipeline that
   forward-fills the last known positioning through that window is asserting that positioning did
   not change during a period of significant market activity. A pipeline that drops the rows
   silently deletes a regime from the sample. Both are wrong; the correct state is an explicit
   `UNAVAILABLE`, which is the same requirement the seed pack states for missing feeds and the
   same one the blocked-egress finding raises.

The general lesson generalises past COT: **any government-published series inherits the
publication calendar's failure modes, including shutdowns.** That applies to CPI, NFP and every
other release Aurum's macro layer consumes. The 2025 shutdown is a labelled natural experiment
for testing whether the desk's macro layer degrades gracefully when a scheduled release simply
never arrives.

## WHAT AURUM ALREADY HAS

§21 lists CFTC positioning; §24 already demands EVENT_TIME / PUBLICATION_TIME / FIRST_SEEN_TIME /
PROCESS_TIME separation — which is **exactly the right schema** and would prevent this entirely
if applied to COT.

## WHAT IS ACTUALLY NEW

- §24's latency schema exists as doctrine; this packet supplies the **concrete case where it is
  most often violated** and a dated window to test against.
- The **irregular backlog offset**, which defeats the obvious constant-offset fix.
- The reframing of the 2025 shutdown as a **free chaos-engineering test case** for the whole
  macro ingestion layer, not just COT.

## ECONOMIC DECISION AFFECTED

macro interpretation · regime · research validity · any positioning-extreme signal

## TESTABLE HYPOTHESIS

H1: Inspect the COT ingestion. Prediction: rows are keyed by report date, and publication
datetime is either absent or synthesised as a constant offset.
H2: Re-run any COT-dependent challenger under correct point-in-time stamping. Prediction:
measured effect shrinks; if it disappears entirely, the effect was leakage.
H3: Replay the 1 Oct – 12 Nov 2025 window through the macro layer. Prediction: some component
forward-fills or silently drops rather than reporting UNAVAILABLE.

## CHEAPEST VALID TEST

H1 is a schema inspection — minutes. H3 is a replay over a known date range on stored data. No
external data required; the CFTC publication calendar is free and public.

## FALSIFICATION CRITERIA

If ingestion already stores true publication datetimes and handles the gap as UNAVAILABLE, close
this packet as already-solved and record it, so no future agent re-raises it.

## OVERFIT / LEAKAGE RISKS

This packet *is* a leakage control. Its own risk is over-correction: aggressively lagging every
macro series "to be safe" destroys genuine information and biases toward WAIT, which §33 and the
seed pack's own non-negotiables warn against by naming excessive-WAIT cost and wrong refusals as
tracked failure modes.

**EXPECTED INFORMATION GAIN:** MEDIUM · **EXPECTED ECONOMIC VALUE:** MEDIUM
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P1
**RECOMMENDED STATUS:** BUILD (point-in-time stamping) · NEGATIVE_CONTROL (H2)

## CHEAPER ALTERNATIVE

Do not build a general point-in-time data warehouse to solve this. Add one column —
`first_available_at` — to the existing COT table and filter on it at query time. That is the
whole fix, and it extends to other series one column at a time.

## CONFIDENCE

High. The lag and the suspension are matters of public regulatory record; only Aurum's exposure
to them is unknown.
