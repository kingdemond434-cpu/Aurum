---
title: EFP blow-outs decouple COMEX from loco-London spot — the anti-condition for every futures-proxy feature Aurum uses
source: reporting on the Jan-Apr 2025 tariff-driven gold EFP dislocation (BullionVault, ANZ Research, Goldmoney); LBMA GOFO discontinuation
source_type: forum
language: en
evidence_grade: E1
claim: During physical-logistics stress the COMEX/London gold basis widens far beyond carry, so the assumption that GC is a clean proxy for XAUUSD breaks exactly when volatility is highest, and Aurum needs an explicit dislocation state that disables futures-derived inference rather than silently degrading it.
mechanism: The EFP is normally arbitraged to financing cost because metal can move freely between vaults. When a tariff, sanction, freight or vault-capacity constraint makes physical delivery into New York costly or uncertain, the arbitrage stops binding and the spread reprices to the cost of the constraint, not to interest rates. Both prices remain valid quotes for two now-different assets — metal in London and metal in New York — so a model treating them as one asset receives a spread that is signal about logistics and reads it as signal about direction.
conditions: Applies whenever the futures/spot spread exceeds plausible carry, when lease rates spike, or when large vault outflows from London to New York are reported; the state is episodic and can persist for months.
anti_conditions: In normal conditions the basis is well-behaved and futures proxies are sound, so this must be a gated state and not a permanent discount on COMEX data; the cited 2025 magnitudes are from market reporting rather than exchange data and must be re-derived from prices before use.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** GOLD-EFP-DISLOCATION-004
**DATE:** 2026-08-13 · **EVIDENCE_GRADE:** E1

## SOURCE CLAIMS — the 2025 episode

Reported by market sources, **not verified against exchange data**:

- Ahead of anticipated US tariffs, the gold EFP widened to roughly **$50/oz**; New York premiums
  reached about **$40/oz** over London.
- Gold lease rates spiked, with one-month rates reported as high as **~5%** in January 2025
  against a normal level near zero.
- A reported **151 tonnes** left London vaults for New York in January 2025 alone.
- On the April 2025 announcement of a gold tariff exemption, the EFP compressed and flows reversed.

## OBSERVED FACT — a data-availability constraint

**LBMA stopped quoting GOFO in January 2015** following the LIBOR reforms. Gold lease rates are
therefore *no longer derivable from public data*. Any design that assumes a lease-rate feed
exists is unbuildable as specified. Third-party derived series exist (e.g. Monetary Metals'
MM GOFO) but are a vendor's model output, not a market print, and must be graded accordingly.

## MY INFERENCE — why this is the highest-leverage item in this batch

§21 lists "basis" as one input among many. That framing is wrong in a way that costs money.
The basis is not a feature; in stress it is a **regime switch on the validity of an entire data
source**. Concretely:

- The standard fix for the tick-volume problem (filed separately) is "use GC volume and delta as
  a proxy for spot". That proxy's validity is exactly what an EFP dislocation destroys. So
  Aurum's two COMEX-facing weaknesses are **correlated**: the workaround for one fails in the
  regime created by the other.
- Aurum's `COMEX` doctrine lens spoke on **all 28 states** in the 2026-08-13 daily and abstained
  zero times. §26 states plainly that "a lens that never abstains has no doctrine." A COMEX lens
  with no dislocation state has no mechanism by which it *could* abstain — it is structurally
  incapable of recognising the one condition under which its own inputs are invalid.

That last point is the finding. The abstention gap is not a tuning issue; it is a missing state.

## WHAT AURUM ALREADY HAS

§21 names basis and "COMEX / futures intelligence"; §25 models execution friction. The desk knows
spot and futures differ.

## WHAT IS ACTUALLY NEW

- The **dislocation state itself** as a first-class, gating market state.
- The observation that it is the **anti-condition for the futures-proxy workaround**, which is
  otherwise the recommended remedy elsewhere in the research programme.
- The **GOFO discontinuity** — a hard, dated limit on what lease-rate data can ever be obtained.
- A cheap, robust **proxy for the state that needs no vault data**: the basis itself, compared
  against a carry estimate from observable rates.

## ECONOMIC DECISION AFFECTED

regime · direction (via corrupted cross-market inference) · execution · macro interpretation ·
research validity of every COMEX-derived feature

## TESTABLE HYPOTHESIS

H1: Define `basis_z` = (GC front-month − XAU spot) minus estimated carry from observable rates,
standardised. During Jan–Apr 2025 `basis_z` exceeds its historical range by a wide margin, giving
a detector that requires no vault-flow or lease-rate feed.
H2: In `basis_z`-extreme windows, the measured lead/lag between GC and XAU differs significantly
from normal windows — i.e. cross-market inference degrades exactly when the detector fires.
H3 (falsification of a tempting trade): The dislocation is **not** directionally tradable in
XAUUSD. The spread moves; spot direction need not. Market reporting at the time described the
EFP panic as leaving gold and silver *prices* largely unmoved.

## CHEAPEST VALID TEST

H1 needs only front-month GC, XAU spot and a short rate — all already available or free. The
2025 episode is a **labelled natural experiment sitting in stored history**: the detector can be
validated against a known event without waiting for the next one.

## FALSIFICATION CRITERIA

If `basis_z` fails to separate Jan–Apr 2025 from normal periods, the cheap detector does not work
and the state needs real vault/flow data — at which point cost/benefit likely says drop it and
simply widen uncertainty when the raw basis is anomalous.

## OVERFIT / LEAKAGE RISKS

Serious and specific: the 2025 episode is **one event**. A threshold tuned to separate it will
separate it. The honest use is as a *detector validated on a known label*, with the threshold
set from the pre-2025 distribution only — never from the episode itself. H3 must be tested with
equal seriousness to H1, because the seductive error is to convert a logistics signal into a
directional trade.

**EXPECTED INFORMATION GAIN:** HIGH · **EXPECTED ECONOMIC VALUE:** MEDIUM-HIGH
**IMPLEMENTATION COST:** LOW · **RUN COST:** LOW · **PRIORITY:** P0
**RECOMMENDED STATUS:** BUILD — as a gating state and an abstention trigger for the COMEX lens

## CHEAPER ALTERNATIVE

No vault-flow feed, no lease-rate vendor subscription, no customs-data ingestion. One derived
scalar from prices Aurum already has, used to *suppress* confidence rather than to *generate*
signals. Suppression is cheap and its failure mode is a missed trade, not a loss.

## RELATED AURUM COMPONENTS

COMEX lens (abstention) · cross-market intelligence · regime router · execution-friction layer

## CONFIDENCE

High that the mechanism is real and recurs — it has recurred under sanctions, freight shocks and
tariffs. Low on the specific 2025 magnitudes, which are secondary reporting.
