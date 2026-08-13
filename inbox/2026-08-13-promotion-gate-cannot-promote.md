---
title: Nine of ten challengers have effective sample size zero and every q-value is 1.000 — the promotion gate currently cannot promote anything
source: Aurum's own record — cards/run-card-2026-08-13.md evidence table
source_type: ai_analysis
language: en
evidence_grade: E1
claim: With ESS 0.0 on nine of ten challengers, all means at +0.000 and all q-values at 1.000, the promotion gate has no path to a positive verdict, so the desk is accumulating run cost without accumulating decision-relevant evidence and the bottleneck is evidence generation rather than gate strictness.
mechanism: A multiple-testing-corrected gate needs effective sample size to move a q-value. If paired outcomes accrue to only one challenger, the other nine can run indefinitely at full token cost while remaining statistically indistinguishable from doing nothing. The system will look busy — rows, signals, states all climbing — while the quantity that determines promotion stays pinned at its null.
conditions: Applies while challengers are instrumented such that paired outcomes route to a single challenger; a power calculation is needed to state how long the current accrual rate would take to resolve anything.
anti_conditions: If the challengers were only recently registered then ESS 0.0 is expected and this is a start-up transient, not a defect; the claim is also void if paired outcomes are being recorded elsewhere and the run card's evidence table is only a partial view.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** AURUM-GATE-POWER-006
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E1 · **SOURCE:** Aurum's own forward record

## OBSERVED FACTS — from `cards/run-card-2026-08-13.md`

```
analogue-memory-v1        ess     0.0  mean +0.000  q 1.000  OBSERVED
consolidation-shelf-v1    ess     0.0  mean +0.000  q 1.000  OBSERVED
doctrine-lenses-v1        ess     0.0  mean +0.000  q 1.000  OBSERVED
entry-method-v1           ess     0.0  mean +0.000  q 1.000  OBSERVED
execution-stress-v1       ess     0.0  mean +0.000  q 1.000  OBSERVED
failed-continuation-v1    ess     0.0  mean +0.000  q 1.000  OBSERVED
gold-ceo-v1               ess     0.0  mean +0.000  q 1.000  OBSERVED
risk-time-allocator-v1    ess  2688.0  mean +0.000  q 1.000  OBSERVED
risk-time-management-v1   ess     0.0  mean +0.000  q 1.000  OBSERVED
user-twin-v1              ess     0.0  mean +0.000  q 1.000  OBSERVED
```

Also recorded: challengers 10, paired outcomes 2688, shadow rows 66547, promoted none. The card's
own L3 line already asks the right question: *"zero promoted — check whether the blocker is
evidence or the gate."* This packet answers it.

## MY INFERENCE — the blocker is evidence, and the arithmetic says so

**All 2688 paired outcomes belong to one challenger.** `risk-time-allocator-v1` has ESS 2688.0;
every other challenger has ESS exactly 0.0. Nine challengers are not "waiting for more data" —
they are receiving *none*. That is a plumbing state, not a statistical one, and no amount of
additional runtime will change it.

Two further observations sharpen this:

1. **The one challenger with 2688 paired outcomes still shows mean +0.000 and q 1.000.** A mean of
   exactly +0.000 at n=2688 is worth inspecting on its own: either the effect is genuinely nil, or
   the pairing is comparing a challenger against itself, or the outcome field is not being
   populated. All three are distinguishable in an afternoon; two of the three are bugs.

2. **The ledger inflation figure points the same way.** The daily reports 20,465 raw rows against
   49 distinct signals — 417.7×. §17 already warns not to mistake raw ledger rows for independent
   sample size. The run card's 66,547 shadow rows are therefore *not* 66,547 units of evidence,
   and the gap between "shadow rows" and "paired outcomes" (2,688) is the real measure of how much
   of the recorded volume is decision-relevant. It is about 4%.

**A power calculation is the missing artefact.** Before adding any new challenger, the desk should
be able to state: at the current accrual rate of paired outcomes per challenger per day, and with
the effect size worth detecting, how many days until q falls below threshold? If that number
exceeds the horizon over which the market regime is stable, the gate is not conservative — it is
**unfalsifiable**, and every challenger registered against it is research theatre.

This is the anti-complexity rule (§50) applied to the evidence system itself.

## WHAT AURUM ALREADY HAS

§17 on ledger-row inflation; §19's promotion pipeline; §13's caution that tail-concentrated
results with small samples are not established edge. The run card already flags zero promotions.

## WHAT IS ACTUALLY NEW

- The **diagnosis**: 2688/2688 outcomes to one challenger identifies this as an instrumentation
  failure, not a strictness problem — which is the opposite of the natural response (loosen the gate).
- The **power calculation as a registration precondition**: a challenger that cannot reach
  significance within the regime's stability horizon should not be registered at all.
- The **4% decision-relevant fraction** as a standing efficiency metric, directly serving §59's
  "fewer resources per unit of useful intelligence".
- The **mean exactly +0.000 at n=2688** as its own bug signal.

## ECONOMIC DECISION AFFECTED

research throughput · promotion · run cost · every capability awaiting evidence

## TESTABLE HYPOTHESIS

H1: Instrumentation — for each of the nine ESS-0.0 challengers, trace why no paired outcome is
recorded. Prediction: they never emit a decision on the same states the baseline does, so pairing
finds nothing to pair.
H2: Power — at the observed accrual rate, time-to-resolution for a plausible effect size exceeds
the regime stability horizon for most challengers.
H3: The mean of exactly +0.000 at n=2688 arises from a degenerate pairing (challenger compared
against itself) rather than from a true null.

## CHEAPEST VALID TEST

All three are inspections of data the desk already holds. No market data, no waiting, no external
sources. H1 and H3 are afternoon tasks; H2 is a closed-form calculation.

## FALSIFICATION CRITERIA — and a caveat that materially weakens this packet

If the nine challengers were registered within the last day or two, ESS 0.0 is simply a cold
start and this packet should be downgraded to "recheck in two weeks".

**This caveat is live, not hypothetical.** This knowledge repository's first commit is dated
2026-08-12 — one day before the run card. If the production challenger registry is of similar
age, then ESS 0.0 across nine challengers is exactly what a cold start looks like and most of the
inference above is premature. The knowledge repo's age does **not** settle the question, because
the challengers live in the production system (`data/promotion_state.json`), not here.

So the ordering is: **check the challenger registration timestamps first.** If they are days old,
file this packet as "recheck 2026-08-27" and stop. Only if they are weeks old does the
instrumentation diagnosis hold. The two observations that survive either way are the ledger
inflation ratio (417.7×, which §17 already flags) and the mean of exactly +0.000 at n=2688,
which is not a cold-start artifact and should be inspected regardless.

## OVERFIT / LEAKAGE RISKS

None — this is instrumentation. The risk it addresses is the costliest kind: a system that
generates the *appearance* of evidence at 66k rows/day while producing ~4% decision-relevant
output and 0% promotion, which is indistinguishable from progress on any dashboard.

**EXPECTED INFORMATION GAIN:** HIGH · **EXPECTED ECONOMIC VALUE:** HIGH
**IMPLEMENTATION COST:** LOW · **RUN COST:** NEGATIVE · **PRIORITY:** P0
**RECOMMENDED STATUS:** BUILD — fix pairing before registering any new challenger

## CHEAPER ALTERNATIVE

Do not add challengers, agents or lenses until pairing works. Every capability in this research
batch is worthless while ESS stays at zero — including the ones in this same drop. **This packet
should be actioned before the other nine notes in this batch**, because none of them can be
tested until the evidence system can measure anything at all.

## RELATED AURUM COMPONENTS

promotion gate · challenger registry · `data/challenger_outcomes.jsonl` · `data/promotion_state.json`

## CONFIDENCE

High. The numbers are the desk's own and the 2688-to-one-challenger split admits few readings.
