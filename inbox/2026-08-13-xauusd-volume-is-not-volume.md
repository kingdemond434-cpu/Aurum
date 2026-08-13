---
title: XAUUSD "volume" is a broker tick count, not traded volume — every volume-derived Gold feature may be measuring quote frequency
source: MQL5 platform documentation and practitioner references on tick volume; CME GC volume reporting
source_type: forum
language: en
evidence_grade: E1
claim: Volume on an XAUUSD chart is the count of price updates in the broker's own feed, not quantity transacted, so any Aurum feature conditioned on XAU volume is conditioned on quote-update frequency and will behave differently across brokers and across liquidity conditions.
mechanism: Spot gold is OTC with no consolidated tape, so no participant can observe traded quantity; a platform must substitute tick count. Tick count rises with quote churn — which increases with volatility, with number of liquidity providers streaming, and with spread widening — so a "volume spike" can occur with zero incremental transacted metal, and a genuine large transaction leaves no trace at all.
conditions: Applies to any XAUUSD/CFD feed including MT4/MT5 and most retail charting; applies wherever a volume, volume-profile, volume-spike, effort-vs-result or acceptance feature reads from the spot symbol.
anti_conditions: Does not apply to CME GC/MGC, where reported volume is genuine exchange-matched quantity; does not apply if the desk's broker publishes true executed quantity; the claim also weakens if tick count turns out to be a high-fidelity proxy for GC volume over the horizons Aurum actually uses, which is the test below.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** XAU-VOL-TICKCOUNT-001
**DATE:** 2026-08-13 · **LANGUAGE:** en · **SOURCE_TYPE:** platform documentation + practitioner
**EVIDENCE_GRADE:** E1 for the empirical proxy quality; the mechanism itself is E5-equivalent
(it follows from market structure, not from a study)

## OBSERVED FACTS

- Spot gold trades OTC. There is no consolidated tape and no central record of transacted
  quantity. This is structural, not a data-vendor limitation.
- MT4/MT5 expose `volume` on non-exchange symbols as **tick volume** — the number of quote
  updates within the bar. `real_volume` is populated only for exchange-traded instruments.
- The tick stream is the **broker's own** aggregated feed. Two brokers pricing the same metal
  produce different tick counts for the same minute.
- CME GC volume is genuine matched quantity and is published daily, free, in the CME Daily
  Bulletin (Section 02B, metals futures and options).

## SOURCE CLAIMS

Practitioner references state the standard workaround is a "futures proxy": read volume and
delta from CME GC and apply the inference to spot. That workaround is itself untested for
Gold at Aurum's horizons and inherits the EFP-dislocation failure mode filed separately.

## MY INFERENCE

This is a **negative control of unusual reach**. Aurum's §9 acceptance logic and §10 market-shape
list both name volume/order flow "where available". If any implementation quietly reads spot
tick volume as if it were the "where available" case, then acceptance, effort-vs-result,
climax detection and volume-profile locations are all conditioned on a variable whose physical
meaning is *how often the broker republished a price*. That correlates with volatility — so
such features will appear to work in backtest through a volatility channel, and will be
attributed to a volume mechanism that does not exist.

The failure is not "the feature is noisy". It is "the feature is a volatility proxy wearing a
volume label", which corrupts *mechanism attribution* — and mechanism attribution is what §19's
promotion pipeline gates on.

## WHAT AURUM ALREADY HAS

§21 lists "GC/MGC price/volume/OI" and "real futures bid/ask/depth where accessible" — so the
desk knows futures volume is the real one. A `COMEX` doctrine lens exists and spoke on all 28
states in the 2026-08-13 daily.

## WHAT IS ACTUALLY NEW

The **audit obligation and the negative control**, not the fact. Specifically: enumerate every
feature in the codebase that reads a volume field, and prove for each one which symbol it
resolves to. The prediction is that at least one path resolves to the spot symbol. Also new:
tick count is a function of *quote-update policy*, which changes when the broker adds or drops
a liquidity provider — an unannounced, undated regime break in a feature the desk believes is
stationary.

## ECONOMIC DECISION AFFECTED

selection · entry · acceptance/failed-acceptance · management (climax detection) · research
(mechanism attribution)

## TESTABLE HYPOTHESIS

H1: Over M5 bars during CME RTH, correlation between broker XAU tick volume and GC matched
volume is < 0.7, and drops materially during spread-widening episodes.
H2: Any Aurum feature currently reading XAU tick volume retains ≥ 90% of its measured effect
when tick volume is replaced by realised volatility over the same bar — i.e. it carries no
volume information beyond volatility.

## CHEAPEST VALID TEST

No new data purchase. Grep the codebase for volume field reads and record the resolved symbol
per call site. Then regress feature output on realised volatility. **H2 is a pure code-and-
arithmetic test on data already stored** — hours of work, no feeds, no market waiting.

## FALSIFICATION CRITERIA

If tick volume retains significant incremental information over realised volatility at the
horizons used, the concern is void and tick volume should be kept, relabelled as
`xau_quote_intensity` so no future reader mistakes it for quantity.

## OVERFIT / LEAKAGE RISKS

Low — this is a measurement-validity audit, not a new signal. The leakage risk it *removes* is
substantial: features that look robust across a backtest because volatility clustering is
persistent.

**EXPECTED INFORMATION GAIN:** HIGH · **EXPECTED ECONOMIC VALUE:** MEDIUM-HIGH
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P0
**RECOMMENDED STATUS:** NEGATIVE_CONTROL — audit before any further volume feature is built

## CHEAPER ALTERNATIVE

Do not build a GC order-flow ingestion layer to fix this. First run H2. If spot tick volume is
just volatility, delete the feature class; deleting is cheaper than replacing.

## RELATED AURUM COMPONENTS

acceptance/failed-acceptance engine · market-shape engine · COMEX lens · `consolidation-shelf-v1`
and `failed-continuation-v1` challengers

## CONFIDENCE

High on the mechanism (it follows from OTC structure). Unknown on how much of Aurum is actually
exposed — that is exactly what the audit measures.
