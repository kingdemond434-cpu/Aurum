---
title: "Prior day high/low" is defined by the broker's server timezone, so the location engine's most-used levels are partly broker artifacts
source: MT4/MT5 broker server-time conventions; MQL5 and practitioner documentation on GMT+2/GMT+3 offsets
source_type: forum
language: en
evidence_grade: E1
claim: The daily bar boundary on a retail Gold feed is set by broker server time rather than by any market event, so prior-day high/low, session high/low and daily-range levels differ between brokers and shift twice a year at DST, making a subset of Aurum's location levels non-reproducible artifacts rather than levels other participants can see.
mechanism: A level matters only if enough participants compute the same number and act there. A boundary chosen so the broker gets five clean daily candles per week is a bookkeeping convention, not a coordination point. Where the convention happens to coincide with the 17:00 New York close it may coordinate; where it does not, the level is private to that feed and any measured reaction is either coincidence or is really a reaction to a nearby genuine level.
conditions: Applies to every level derived from a calendar-day or session boundary on a broker feed — prior day high/low, prior day close, daily open, session high/low, and any "revisit count" or "freshness" attribute computed over daily bars.
anti_conditions: Does not apply to levels defined by an exogenous event (macro release extreme, LBMA auction print, futures settlement, swing highs/lows), which are boundary-independent; the concern also weakens for a broker whose boundary already sits at 17:00 New York, where the convention and the real daily reset coincide.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** XAU-LEVEL-BOUNDARY-002
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E1

## OBSERVED FACTS

- Most MT4/MT5 brokers run servers at **GMT+2 in winter and GMT+3 in summer**, chosen so the
  week produces exactly five D1 candles with no stub bar at the Sunday open. That offset places
  the daily boundary at the 17:00 New York close.
- Brokers do not agree on *which* DST calendar to follow. Some switch on the US schedule, some
  on the EU schedule. The two differ by roughly three weeks in spring and one week in autumn.
- Any other offset yields **six** daily candles in a five-day week, changing every daily-derived
  level.

## MY INFERENCE

Three distinct failure modes, only the first of which is widely appreciated:

1. **Cross-broker level disagreement.** Aurum's prior-day levels are computed on its own feed.
   If the desk's feed differs from the venue where the bulk of participants coordinate, the
   level is private. §8 requires the location engine to answer "WHY HERE?"; "because my broker
   starts its day here" is not an acceptable answer, yet it is currently an unfalsified one.

2. **A silent regime break twice a year.** For the ~3 weeks between US and EU DST transitions,
   the daily boundary moves by one hour relative to the market. Every daily-derived level shifts.
   Nothing in the system logs this as an event. A challenger's performance can step down across
   that window and be attributed to "regime change" when it is a clock change.

3. **Level-attribute corruption.** §8 gives levels attributes — age, revisit count, freshness.
   All of these are computed *over daily bars*. A boundary shift does not just move one level; it
   re-partitions the entire history from which age and revisit count are derived. So the attributes
   Aurum uses to rank level importance are themselves boundary-dependent.

## WHAT AURUM ALREADY HAS

§8's location engine already treats levels as objects with attributes and explicitly rejects
"price reached resistance" as reasoning. §21 tracks "session/holiday state".

## WHAT IS ACTUALLY NEW

That the level's **coordinate system** is an unvalidated free parameter. Aurum interrogates
which level matters and why; it does not appear to interrogate whether the level is the same
number another participant would compute. This is a layer beneath the location engine's current
ontology — §48's "what structural state has no representation?" answers: *the clock the structure
is measured against*.

## ECONOMIC DECISION AFFECTED

selection · entry · WAIT · regime attribution · research validity

## TESTABLE HYPOTHESIS

H1: Recompute prior-day high/low under three boundaries — broker server time, 17:00 America/New_York
with correct US DST, and 00:00 UTC. On the desk's stored history, the three disagree on a material
fraction of days, and reaction statistics at the level differ by boundary.
H2: The **17:00 New York** boundary produces the strongest reaction statistics, because it
coincides with the CME daily settlement/reset that futures participants actually coordinate on.
H3: Challenger performance is measurably different inside the US/EU DST mismatch windows.

## CHEAPEST VALID TEST

Entirely offline on stored bars. Recompute levels under three boundaries, measure touch-reaction
statistics under each. No new data, no waiting. **H3 costs one date filter.**

## FALSIFICATION CRITERIA

If the three boundaries produce statistically indistinguishable reaction statistics, the level's
coordinate system does not matter and this packet should be rejected and recorded as such.

## OVERFIT / LEAKAGE RISKS

Testing three boundaries is three trials — small, but it must be logged in the trial registry so
the deflated-Sharpe correction counts it. Choosing the best-performing boundary post hoc and then
using it *is* the overfit; the defence is that H2 is a **preregistered directional prediction with
a mechanism** (settlement coordination), not a search.

**EXPECTED INFORMATION GAIN:** HIGH · **EXPECTED ECONOMIC VALUE:** MEDIUM-HIGH
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P0
**RECOMMENDED STATUS:** BUILD — as a measurement fix, not a new signal

## CHEAPER ALTERNATIVE

Do not build a multi-broker level-consensus service. Pin one boundary — 17:00 New York with
explicit US DST — record it as a versioned config constant, and emit a log event on every DST
transition so it is visible in the record. That captures most of the value for near-zero cost.

## RELATED AURUM COMPONENTS

location engine · level attributes (age/freshness/revisit) · session state · every challenger
whose entry references a daily level

## CONFIDENCE

High that the boundary varies and that levels differ. Unknown which boundary is economically
best — H2 is a prediction, not a finding.
