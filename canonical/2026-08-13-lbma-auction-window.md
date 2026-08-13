---
title: The LBMA auction windows are scheduled liquidity events, and the 2014 leakage finding is a falsification target rather than an edge
source: Caminschi & Heaney (2014), Journal of Futures Markets 34, 1003-1039; LBMA and ICE Benchmark Administration documentation
source_type: paper
language: en
evidence_grade: E2
claim: Gold's two daily benchmark auctions at 10:30 and 15:00 London are scheduled events with documented elevated volume and volatility during the auction itself, and a published 2014 study found trading during the pre-publication window was directionally informative — but the auction was reformed onto an electronic platform in 2015, so the leakage finding is most likely dead and should be treated as a falsification exercise.
mechanism: A benchmark auction concentrates real hedging and index-tracking demand into a short window, which mechanically raises volume and volatility regardless of any information leak. Separately, if participants observe order imbalance during the auction before the result is published, that observation is tradeable in derivative markets — a channel that widening participation and electronic administration are designed to close.
conditions: The volume and volatility concentration should persist as long as the auctions exist and set contractual reference prices; the leakage channel is conditional on the pre-2015 telephone-based structure.
anti_conditions: Post-2015 reform moved administration to ICE Benchmark Administration with more direct participants and greater transparency, which plausibly eliminated the leak; any post-2015 replication showing predictability should be suspected of multiple-testing artifact before it is believed.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** GOLD-LBMA-AUCTION-008
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E2 for the study; E5 for the institutional facts

## OBSERVED FACTS

- The **LBMA Gold Price** is set twice daily at **10:30 and 15:00 London time**, in USD per fine
  troy ounce of 995 gold, via auctions administered by **ICE Benchmark Administration**.
- The auction is iterative: a chairperson sets an opening price, participants enter buy and sell
  volumes, and if the imbalance exceeds tolerance the price is adjusted and another round runs
  until it clears. Multiple rounds mean the auction has **duration**, not a single instant.
- The benchmark is a contractual reference price for physical settlement, ETFs and derivatives —
  so demand at the auction is partly **non-discretionary**.

## SOURCE CLAIM — the study

Caminschi, I. & Heaney, R. (2014), *Fixing a Leaky Fixing: Short-Term Market Reactions to the
London PM Gold Price Fixing*, **Journal of Futures Markets 34, 1003–1039**. Peer-reviewed.

Reported findings: significantly elevated trade volume and price volatility in GC futures and GLD
**immediately after the fixing began and before its result was published**; and orders placed
during the meeting were most directionally accurate on the days price moved most afterwards. The
authors describe this as "highly suggestive of information leaking from the fixing".

## MY INFERENCE

The honest reading is that this paper's **headline result is probably obsolete** and its
**institutional description is not**. Those two halves deserve different treatment:

- The leakage channel described is exactly what the 2015 reform targeted — moving from a
  telephone call among a handful of banks to an electronic auction with wider, supervised
  participation. Assuming the leak survived its own remedy is the kind of folklore §35 warns about.
- The **volume and volatility concentration** is mechanical and should survive any reform,
  because it comes from non-discretionary benchmark-referencing demand, not from misconduct.

So the value here is not a signal. It is a **calendar fact with a mechanism**: twice a day, at
known times, real non-speculative flow concentrates. That matters to Aurum in a specific way that
has nothing to do with direction — §24's information-latency doctrine and §25's execution reality.
A structural level tested at 15:00 London is being tested partly by flow that is *indifferent to
the level*. Treating that test as evidence of acceptance or rejection, in §9's sense, mistakes
mandated flow for informed flow.

That is the novel decision-relevant claim: **the acceptance engine should know that some level
tests are not opinions.**

## WHAT AURUM ALREADY HAS

§21 tracks session state; §24 handles information latency; §9 defines acceptance and failure
evidence. The desk has a WYCKOFF lens concerned with auction theory.

## WHAT IS ACTUALLY NEW

- The two auction timestamps as **marked, mechanism-backed events** rather than generic "session"
  boundaries.
- The distinction between **mandated flow and informed flow** as an input to acceptance scoring —
  a discrimination Aurum's §9 evidence list does not currently make.
- A **peer-reviewed citation** for a gold-specific microstructure effect, which is rarer than the
  volume of gold commentary suggests, together with the reason to expect it no longer holds.

## ECONOMIC DECISION AFFECTED

acceptance/failed-acceptance scoring · WAIT · execution timing · entry quality

## TESTABLE HYPOTHESIS

H1 (mechanical, expected true): Realised volatility and range in the 10:30 and 15:00 London
windows exceed matched control windows on the same day, on stored history.
H2 (the discriminating one): Level tests occurring **inside** the auction windows have
**lower** subsequent follow-through than level tests outside them — i.e. auction-window
rejections and breaks are less informative about continuation.
H3 (falsification of the 2014 result post-reform): Direction of the auction-window move has **no**
predictive association with the subsequent move in the post-2015 sample. Expected outcome: null.

## CHEAPEST VALID TEST

All three run on stored M1/M5 bars with two timestamps per day. No feeds, no vendor, no waiting.
H2 is the one with economic consequence and it is a simple conditional split.

## FALSIFICATION CRITERIA

If H2 shows no difference, auction windows are just ordinary volatile periods and Aurum needs
nothing beyond what it already has. Record and close — this is a likely outcome and should not be
resisted.

## OVERFIT / LEAKAGE RISKS

DST is the trap: London is UTC in winter and UTC+1 in summer, so "15:00 London" is not a fixed UTC
time. A fixed-UTC implementation will be one hour wrong for roughly half the year and will
produce a spurious null. Also note the auctions do not run on London bank holidays.

**EXPECTED INFORMATION GAIN:** MEDIUM · **EXPECTED ECONOMIC VALUE:** MEDIUM
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P1
**RECOMMENDED STATUS:** RESEARCH_ONLY (H2), NEGATIVE_CONTROL (H3)

## CHEAPER ALTERNATIVE

Two timestamps in a calendar table and a boolean feature. Do not build auction-imbalance
ingestion; the imbalance data that made the 2014 result possible is not publicly available in
real time, and the reform was designed to keep it that way.

## CONTRADICTIONS

Sets up a direct test against §9's implicit assumption that a level test is informative about
participant intent. Some level tests are calendar-driven.

## CONFIDENCE

High on institutional facts and on H1. Low-to-moderate on H2, which is the genuinely open
question. Deliberately low on H3 — the expected result is a null, and that null is the finding.
