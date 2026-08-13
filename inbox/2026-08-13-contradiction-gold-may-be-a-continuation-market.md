---
title: "Contradiction candidate: a 2026 preprint reports Gold consolidates breakouts ~66% of the time while FX invalidates them ~75% — the opposite asymmetry to Aurum's reversal preference"
source: Costa, R. (April 2026), "The Illusion of Breakouts: Empirical Evidence of Institutional Liquidity Capture in Major Currency Pairs", SSRN preprint 6592020 — full text NOT accessible from this environment
source_type: paper
language: en
evidence_grade: E1
claim: A single-author SSRN preprint reports that across 2016-2026 and 3,800+ breakout attempts against a 20-day range, major FX pairs invalidated breakouts in over 75% of occurrences while Gold consolidated true breakouts in over 66%, which if true means Gold is structurally a continuation market and Aurum's reversal-selection hypothesis is pointed the wrong way for this instrument.
mechanism: If Gold's flow is dominated by macro-directional and official-sector participants rather than by liquidity-providing algorithms, then a breakout in Gold more often reflects a genuine repricing than an inventory-driven stop sweep, inverting the retail-facing folklore that breakouts are usually traps.
conditions: As reported, the finding is conditional on a 20-day range definition, the 2016-2026 sample, and daily-or-similar resolution; the Gold result rests on one instrument within a six-instrument study.
anti_conditions: The paper is a non-peer-reviewed preprint by a single author whose full text could not be read here, so the breakout definition, the invalidation criterion, transaction costs and any significance testing are all unverified; a 66% "consolidation" rate is not an edge without knowing payoff, and the FX-versus-Gold contrast is exactly the kind of cross-sectional result that arises by chance when six instruments are compared.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** CONTRA-GOLD-BREAKOUT-011
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E1 — deliberately low, see below

## PROVENANCE — read this before the finding

- **Author:** Rodrigo Costa. **Posted:** April 2026. **Venue:** SSRN preprint, abstract ID 6592020.
- **Not peer-reviewed.** Single author. No journal.
- **I could not read the paper.** `papers.ssrn.com` is blocked by this environment's egress proxy.
  Everything below is from the abstract as surfaced in search results.

Under §35 this is a **SOURCE CLAIM**, not a fact. It is filed because §47 rewards contradictions,
not because it is believed.

## SOURCE CLAIMS

- Sample: 2016–2026, instruments EURUSD, GBPJPY, USDCAD, USDJPY, AUDUSD and **Gold**.
- Method: maps a "20-day institutional range"; raw sample of **3,800+ breakout attempts**.
- Result: FX invalidates breakouts and sweeps liquidity in **over 75%** of mapped occurrences;
  **Gold consolidates true breakouts in over 66%** of events, described as a "macroeconomic
  directional flow anomaly".

## WHY THIS MATTERS TO AURUM

§7 sets out the desk's most consequential selection hypothesis: that climactic impulse into an
important location with failed continuation is a **better** environment than orderly grind into a
fresh extreme, and it explicitly asks to be falsified. This preprint is a falsification *pointer*
aimed straight at it. If Gold genuinely consolidates two-thirds of range breakouts, then:

- the reversal family (§5) is being applied to the instrument least suited to it;
- the continuation family (§6) deserves more of the desk's selection budget, not less;
- the §7 asymmetry may be an artifact of the **user's trade history**, which is a small,
  self-selected sample of trades the user chose to take — precisely the bias §27's digital-twin
  work is meant to expose rather than inherit.

That last point is the one worth dwelling on. §7's hypothesis was derived from historical
user-trade analysis. A trader who prefers reversals will have a trade history full of reversals,
and the reversals they took will be the ones they judged good. Deriving "reversals are the better
environment" from that sample is close to circular. The preprint does not prove the hypothesis
wrong — but it is an independent-sample reason to suspect the derivation.

## WHY I DO NOT BELIEVE IT YET

- Single-author preprint, no peer review, no replication, full text unread.
- "Consolidating true breakouts in over 66%" contains a possible tautology: if "true breakout" is
  defined by whether it consolidated, the statistic is circular. **I cannot check this without the
  text, and it is the first thing to check.**
- Six instruments compared, one result highlighted as anomalous. That is a multiple-comparison
  setup.
- No mention of transaction costs. §25 requires net-of-cost evaluation.
- A hit rate is not an edge. 66% with poor payoff loses money; 40% with good payoff makes it.

## WHAT IS ACTUALLY NEW

Not the paper — the **test it justifies**. Aurum can settle this on its own data, at zero external
cost, without needing the preprint to be correct or even readable. The paper's only real function
is to make the test worth prioritising.

## ECONOMIC DECISION AFFECTED

selection · direction · the §7 hypothesis · reversal-versus-continuation budget allocation

## TESTABLE HYPOTHESIS

H1 (direct replication on Aurum's own history): Define a 20-day range on XAUUSD. Classify each
breakout by whether price sustains beyond the boundary over a fixed forward window. Measure the
sustain rate. Prediction under the preprint: > 50%, materially above what the desk's reversal
preference implies.
H2 (the one that matters economically): Sustain **rate** and expectancy **net of costs** are
different questions. Compute both. A high sustain rate with poor payoff changes nothing.
H3 (the circularity check): Run H1 under at least two independent definitions of "true breakout",
one of which does not reference subsequent consolidation. If the result is definition-sensitive,
the preprint's headline is an artifact.
H4 (the self-selection check, most valuable): Compare the reversal-versus-continuation base rates
in **market data** against their frequency in the **user's trade history**. If the user's history
over-represents reversals relative to their base rate, §7's hypothesis is partly a description of
the user, not of Gold.

## CHEAPEST VALID TEST

H1 and H3 run on stored daily and intraday bars — no external data, no subscription, no waiting.
**H4 needs only the user's existing trade record**, which the desk already has for `user-twin-v1`.
H4 is the highest information-per-cost item in this entire batch.

## FALSIFICATION CRITERIA

If XAUUSD's sustain rate on Aurum's own data is near or below 50%, the preprint does not replicate
on this instrument and this packet should be rejected and recorded — a documented negative on a
widely-shared intuition is worth keeping.

## OVERFIT / LEAKAGE RISKS

The 20-day range is one parameter among many; testing several lookbacks and reporting the best is
the obvious trap. Fix the lookback at 20 days **because the preprint specifies it**, and treat
any other lookback as a separate logged trial under the trial-counting packet.

**EXPECTED INFORMATION GAIN:** HIGH · **EXPECTED ECONOMIC VALUE:** HIGH if it replicates
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P1
**RECOMMENDED STATUS:** RESEARCH_ONLY — run H4 first, then H1/H3

## CONTRADICTIONS

Directly targets §7. Also sits against the desk's own mined corpus claim that "Gold tends to run
into liquidity above the previous day high before reversing", which asserts the opposite pattern.
Both cannot be generally true; both can be regime-conditional, which is the interesting outcome.

## CONFIDENCE

**Low in the source.** Moderate-to-high that the *question* is live and under-tested at Aurum.
The value of this packet is the H4 self-selection test, which does not depend on the preprint
being right about anything.
