---
title: The gold / real-yield relationship decayed after 2022 — the driver engine's first-listed driver is the one that stopped working
source: multiple market-research secondary sources reporting rolling correlations; WGC and ECB central-bank demand data (primary sources not reachable from this environment)
source_type: ai_analysis
language: en
evidence_grade: E1
claim: The inverse gold / 10-year TIPS-yield relationship that held from roughly 2003 to 2022 weakened to near zero from 2022 onward as price-insensitive central-bank buying replaced rate-sensitive investor flow as the marginal driver, so any Aurum component that assumes real yields are gold's primary driver is calibrated to a dead regime.
mechanism: The real-yield channel works through opportunity cost and requires the marginal buyer to be an investor choosing between gold and a real-yielding asset. Reserve managers buying for diversification away from Treasuries are not making that comparison and are largely price-insensitive within wide bands, so their flow breaks the transmission rather than merely adding noise.
conditions: Applies from roughly 2022 to the present while official-sector purchases remain at multi-year highs; the relationship should re-establish if official-sector demand normalises toward its 2010-2021 average and investor flow becomes marginal again.
anti_conditions: Correlation is not the mechanism — a low rolling correlation can also arise from a third driver dominating both series temporarily, or from the specific real-yield proxy chosen; the cited correlation figures come from secondary market commentary, not from a reproducible study, and must be recomputed before anything is built on them.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** GOLD-DRIVER-REALYIELD-003
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E1 (secondary; primary sources egress-blocked here)

## SOURCE CLAIMS — flagged as claims, not facts

Reported by market-research secondary sources and **not independently reproduced**:

- Rolling 12-month gold / real-yield correlation averaged about **−0.73** from 2003 to ~2022.
- Over 2022–2023 the correlation fell to roughly **+0.03**; since 2024, roughly **+0.07**, with
  gold making new highs while 10-year TIPS yields stayed firmly positive.
- Central bank purchases averaged **~473 t/yr over 2010–2021**, then exceeded **1,000 t/yr in each
  of 2022, 2023 and 2024**.

These numbers should be treated as **pointers to a test, not as inputs**. The primary sources
(gold.org, ecb.europa.eu) are blocked from this environment; FRED series `DFII10` and a gold
series are free and sufficient to recompute the correlation from scratch.

## MY INFERENCE

§22 lists REAL YIELDS **first** among potential dominant Gold drivers, and the desk's own mined
corpus contains "Real yields rising leads to pressure on gold because the opportunity cost of
holding it increases" as a MECHANISM claim. If the correlation figures survive replication, that
claim is *regime-conditional and currently inactive*, and its position at the head of the list is
an ordering inherited from the pre-2022 world.

The deeper point is not "gold's driver changed". It is that **§22 is a static list**. A driver
engine that asks "what is actually driving Gold right now?" cannot answer from a hardcoded
ranking. The correct object is a *rolling, estimated* attribution with an explicit decay test —
and, critically, a state for **"no driver currently dominant"**, which is the honest answer more
often than any list suggests.

## WHAT AURUM ALREADY HAS

§22's driver list already names CENTRAL BANK DEMAND alongside REAL YIELDS, and §20 already
demands regime-conditional testing. So the *ingredients* are present.

## WHAT IS ACTUALLY NEW

Three things:

1. **A specific, dated, testable decay** of the single most-cited gold mechanism — with a
   published pre-period correlation to falsify against.
2. **The measurement frequency mismatch.** Central-bank demand is reported *quarterly, with a
   lag of weeks*. Real yields are continuous. A driver-attribution engine that mixes them
   naively will always underweight the slow driver, because a quarterly step function cannot
   win a rolling-correlation contest against a daily series. This is a genuine estimation
   trap and is separate from the regime claim.
3. **The asymmetry of a price-insensitive buyer.** A price-insensitive bid does not just shift
   the level — it changes the *shape* of dips. If reserve managers accumulate on weakness within
   wide bands, then pullback depth distributions differ between regimes. That is directly
   relevant to §6 continuation logic and to §7's reversal-vs-continuation selection hypothesis,
   and it is a channel by which a macro fact becomes a price-action fact.

## ECONOMIC DECISION AFFECTED

direction · regime · macro interpretation · selection (via pullback-depth distribution)

## TESTABLE HYPOTHESIS

H1: Recomputed from FRED, the rolling 250-day correlation between daily gold returns and daily
changes in 10y TIPS yield is materially more negative pre-2022 than post-2022, and the break is
robust to the choice of real-yield proxy.
H2 (the one that matters): Conditioning any Aurum directional component on real yields adds
**no** measured value post-2022, and adds value pre-2022. If so, the component should hibernate
under §20 rather than be deleted.
H3 (the novel one): Median pullback depth on H4 impulses is shallower in the high-official-demand
regime than in the pre-2022 regime.

## CHEAPEST VALID TEST

H1 and H2 need only free FRED data and stored gold history — a single script, no feeds, no
subscriptions, no waiting. H3 runs on stored bars alone. **All three are same-day, zero-cost.**

## FALSIFICATION CRITERIA

If the rolling correlation shows no break, or the break disappears under a different real-yield
proxy or a different window, reject this packet and record the rejection — the mechanism is
widely believed enough that a documented negative is worth keeping.

## OVERFIT / LEAKAGE RISKS

The 2022 break date is **chosen with hindsight**, which is exactly the structural-break search
that inflates significance. The defence is to use a change-point detection method that estimates
the break date rather than assuming it, and to report the break date's confidence interval. If
the estimated break lands far from 2022, the narrative is wrong even if some break exists.

**EXPECTED INFORMATION GAIN:** HIGH · **EXPECTED ECONOMIC VALUE:** MEDIUM
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P0
**RECOMMENDED STATUS:** RESEARCH_ONLY → BUILD if H1 replicates

## CHEAPER ALTERNATIVE

Do not build a full structural macro model with a factor decomposition. A rolling regression of
gold returns on a small fixed set of daily observables, with the coefficients themselves exposed
to the CEO as *state* (including "no coefficient currently significant"), captures most of the
decision value for a fraction of the cost — and is falsifiable, which a narrative driver list is not.

## CONTRADICTIONS

Directly weakens the desk's own mined MECHANISM claim about real yields and opportunity cost.
That claim should be relabelled regime-conditional rather than deleted.

## CONFIDENCE

Medium-high that the relationship weakened — it is widely reported across independent sources.
Low confidence in the specific correlation values, which are secondary and unreproduced.
