---
title: Purging and embargo are the known half of leakage control — the unknown half is counting every trial that was ever attempted
source: López de Prado on purged/embargoed and combinatorial purged cross-validation; Bailey & López de Prado on the deflated Sharpe ratio; Harvey & Liu on multiple testing in finance
source_type: paper
language: en
evidence_grade: E2
claim: Aurum's promotion gate can only be correctly calibrated if the number of trials it deflates for includes every variant ever attempted and abandoned, not just the challengers formally registered, and no ordinary engineering workflow records the abandoned ones.
mechanism: Multiple-testing corrections take the trial count as an input. Every discarded parameter setting, every feature tried and dropped, every prompt revision, and every hypothesis a research agent explored and did not write up is a trial. Because abandoned work leaves no artifact, the recorded trial count is systematically smaller than the true one, so the correction is systematically too weak and the gate is more permissive than its own statistics claim.
conditions: Applies to any promotion decision that uses a multiple-testing-adjusted statistic; the size of the error grows with the number of research agents and the speed of automated exploration.
anti_conditions: Not binding if the gate's threshold is set so conservatively that plausible trial-count undercounting cannot change the verdict; also weaker where challengers are genuinely preregistered before any data is examined, which is the design the desk already states it wants.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** METHOD-TRIAL-COUNT-010
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E2

## SOURCE CLAIMS — established methodology

- **Purged k-fold CV** removes training observations whose labels overlap in time with the test
  set, and **embargo** additionally removes observations immediately following the test set.
  Without both, a model with multi-bar labels trains on information overlapping its own test data.
- **Combinatorial Purged CV (CPCV)** generates multiple backtest paths and reports lower
  probability of backtest overfitting than walk-forward or standard k-fold.
- **The Deflated Sharpe Ratio** adjusts an observed Sharpe for selection bias, non-normality, and
  **the number of trials conducted**.

## WHAT AURUM ALREADY HAS

§19's pipeline includes OOS and preregistration; §20 explicitly requires "multiple-testing
control". The run card already reports **q-values**, so a correction is implemented. This is
mostly known territory and repeating it would violate §34.

## WHAT IS ACTUALLY NEW — the trial-counting problem

The deflation formula needs **N**, the number of trials. Aurum's registry knows about 10
challengers. But N is not 10. N includes:

- every parameter variant tried during development and abandoned
- every feature engineered, evaluated, and dropped
- every prompt revision that changed a specialist's behaviour and was reverted
- **every hypothesis an external research agent explored and chose not to submit**

That last category is the one this directive creates at scale. A research protocol that invites
many external models to search aggressively and **push only high-information contributions**
(§33, §56) is, statistically, a machine for generating unrecorded trials. The filtering is the
point — and the filtering is exactly what makes N unknowable from the artifacts that survive.

An agent that explores 200 hypotheses and submits the best 10 has performed 200 trials. The
registry sees 10. The deflation is calibrated for 10. **The selection that makes the contribution
valuable is the same selection that invalidates the statistics used to judge it.**

This is a genuine tension in the seed-pack protocol, and it does not have a fully satisfying fix.

## MY INFERENCE — the partial fixes, honestly graded

1. **Require submitted trial counts.** Ask each contributing agent to report how many hypotheses
   it considered, not just those submitted. Cheap, and better than nothing — but self-reported
   and unverifiable, so it establishes a lower bound only.
2. **Set N by the exploration budget, not the submission count.** If an agent had capacity to
   examine ~200 hypotheses, deflate for ~200 regardless of what it submitted. Crude, conservative,
   and requires no cooperation from the contributor. **This is the recommended default.**
3. **Make forward evidence the gate that matters.** A prospectively frozen challenger evaluated on
   data that did not exist when it was written is immune to trial-count inflation, because the
   data cannot have been mined. §18's evidence hierarchy already ranks forward evidence highest —
   the new point is *why*: it is the only gate that is structurally robust to unknown N.

Fix 3 is the real answer and it is already in the doctrine. What is new is the argument that
under a multi-agent research protocol, forward evidence is not merely *preferred* — historical
statistical gates become close to uninterpretable, so forward evidence is the only one that
survives at all.

## ECONOMIC DECISION AFFECTED

promotion · research methodology · how this entire external-contribution protocol is scored

## TESTABLE HYPOTHESIS

H1: Inspect the gate. Prediction: N is the count of registered challengers, so the correction is
anti-conservative by whatever factor development trials exceed registered ones.
H2: Recompute existing q-values with N set to a plausible exploration budget (say 100–500). If
verdicts are unchanged, the issue is moot at current effect sizes and can be closed cheaply.
H3: For every challenger, check that the embargo period **exceeds the maximum holding time** of
the strategy it tests. A runner challenger holding for days needs a days-long embargo; a default
of a few bars silently leaks.

## CHEAPEST VALID TEST

**H2 is a one-line change and answers the whole question.** If the verdicts do not move under an
aggressively large N, stop — the concern is real but not binding, and that is a finding worth
recording. H3 is a config audit.

## FALSIFICATION CRITERIA

If H2 shows verdicts are insensitive to N across a wide range, this packet is theoretically
correct and practically irrelevant. Record it as such rather than building anything.

## OVERFIT / LEAKAGE RISKS

The meta-risk: over-deflating makes the gate impossible to pass, which — given the separate
finding that nine of ten challengers already sit at ESS 0.0 — could convert a plumbing problem
into a permanent one and be misread as appropriate rigour. Fix the pairing first; tune N second.

**EXPECTED INFORMATION GAIN:** MEDIUM-HIGH · **EXPECTED ECONOMIC VALUE:** MEDIUM
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P1
**RECOMMENDED STATUS:** BUILD (H2 + H3 audit)

## CHEAPER ALTERNATIVE

Do not implement CPCV. It is the statistically superior method, but with ESS at 0.0 on nine
challengers the binding constraint is sample generation, not estimator efficiency. CPCV on no data
returns nothing more informative than walk-forward on no data. Revisit once pairing works.

## CONTRADICTIONS

Weakens the seed pack's own protocol: "push only high-information contributions" is a selection
rule that inflates unrecorded N. The protocol should carry this caveat explicitly.

## CONFIDENCE

High on the methodology, which is standard. High on the trial-counting argument, which follows
directly. Unknown whether it binds for Aurum — H2 settles that in one run.
