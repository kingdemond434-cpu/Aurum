---
title: The Shanghai Gold Exchange has its own auction benchmark and a night session that overlaps London — Chinese physical pricing is an unmodelled information surface
source: Shanghai Gold Exchange official rules and product pages (sge.com.cn), Chinese-language market commentary on 内外盘价差
source_type: forum
language: zh
evidence_grade: E1
claim: SGE runs a yuan-denominated gold benchmark auction (上海金, code SHAU) and a 20:50-02:30 Beijing night session, and the SGE-versus-international price differential is a directly observable proxy for Chinese physical demand that Aurum currently does not measure.
mechanism: China is a large physical gold buyer with a partially closed capital account and import quotas, so domestic price cannot arbitrage freely to the international price. The differential therefore carries information about local physical demand and import friction that is not present in any dollar-denominated series, and it is set during hours when the dollar market is thin and price is most susceptible to a marginal physical bid.
conditions: Applies during SGE trading hours, particularly the day session which spans the Asian XAUUSD session; the differential is most informative when driven by demand rather than by a change in import quota policy or a yuan move.
anti_conditions: The differential is contaminated by CNY/USD exchange-rate moves, by import-quota policy changes, and by domestic tax and VAT treatment, so a naive premium series mixes at least four causes; Chinese onshore prices may also simply follow international prices with a lag, in which case the differential is an echo and not a signal.
---

# AURUM KNOWLEDGE PACKET

**KNOWLEDGE_ID:** GOLD-SGE-SESSION-007
**DATE:** 2026-08-13 · **LANGUAGE:** zh · **EVIDENCE_GRADE:** E1

## OBSERVED FACTS — from SGE official sources

- **上海金 (Shanghai Gold Benchmark Price)**, contract code **SHAU**, quoted in **CNY per gram**,
  minimum tick 0.01 yuan. Set by 集中定价交易 (centralised pricing / auction), run as a
  **morning session and an afternoon session**, each resolving through one or more rounds until
  supply and demand match. Governed by the published 《上海黄金交易所集中定价交易细则》.
- **SGE trading hours (Beijing time):** 早盘 08:50–11:30, 午盘 13:30–15:30, **夜盘 20:50–02:30**.
- SGE publishes benchmark prices and daily quotes publicly at sge.com.cn.

## THE SESSION-STRUCTURE FACT THAT MATTERS

Converting to UTC (Beijing = UTC+8, no DST):

| SGE session | Beijing | UTC | Overlaps |
|---|---|---|---|
| 早盘 day | 08:50–11:30 | 00:50–03:30 | **the thin Asian XAUUSD session** |
| 午盘 day | 13:30–15:30 | 05:30–07:30 | Asian close / pre-London |
| 夜盘 night | 20:50–02:30 | 12:50–18:30 | **London PM and the NY session** |

The consequence is not obvious and is the reason this packet exists: the Asian XAUUSD session —
routinely treated as low-information "chop" to be filtered out — is precisely when the **largest
physical gold market in the world is open and the dollar market is not**. If any part of Aurum
suppresses Asian-session signals on liquidity grounds, it may be discarding the window with the
highest ratio of physical-demand information to speculative noise.

Symmetrically, the SGE **night** session means Chinese participants are active *through* the
London and New York sessions, so Chinese flow is not confined to Asian hours.

## MY INFERENCE

Two separable research objects, which should not be conflated:

1. **A state variable** — the onshore/offshore differential (内外盘价差), as a proxy for Chinese
   physical demand and import friction. Slow-moving, macro-ish, days-to-weeks horizon.
2. **A session-structure fact** — SGE benchmark auctions are discrete, scheduled, liquidity-
   concentrating events inside the Asian session. Fast, intraday, directly relevant to §8's
   location engine and to session-boundary definitions.

Object 2 is the cheaper and more novel one for a desk trading intraday XAUUSD. Aurum's location
engine enumerates prior day/week/session highs and lows but has no concept of a **scheduled
auction print in another currency** acting as a reference point during Asian hours.

## WHAT AURUM ALREADY HAS

§21 lists central-bank gold activity and §38 already directs Chinese-language search. §37's
multilingual mandate anticipates this ecosystem. Session/holiday state is tracked.

## WHAT IS ACTUALLY NEW

- The **specific auction mechanism and its two daily prints** as markable events.
- The **UTC session map** and its implication that Asian-session suppression may be discarding
  physical-demand information.
- The **four-cause contamination warning** on the premium series — CNY, quota policy, VAT,
  demand — which is the reason a naive "China premium" feature would fail and which is not
  visible from English-language sources that quote the premium as a single clean number.

## ECONOMIC DECISION AFFECTED

regime · direction (slow horizon) · session handling and WAIT logic (fast horizon) · location

## TESTABLE HYPOTHESIS

H1 (cheap, intraday): XAUUSD realised volatility and directional persistence in the 15 minutes
following each SGE benchmark print differ significantly from matched control windows in the same
session. If not, object 2 is dead and only the slow state variable remains.
H2 (slow): The onshore/offshore differential, orthogonalised against CNY moves, has predictive
association with XAUUSD returns at a multi-day horizon.
H3 (the falsifier that should be run first): The differential is **Granger-caused by** the
international price rather than the reverse — i.e. Shanghai follows London. If so, the premium is
an echo and objects 1 and 2 are both worthless as leading information.

## CHEAPEST VALID TEST

**Run H3 first.** It needs only two published daily price series and a currency series, all free,
and it can kill the entire line of research in a day for near-zero cost. Only if H3 fails to show
pure one-way causation is it worth building any ingestion.

## OVERFIT / LEAKAGE RISKS

Timezone handling is the main leakage vector: Beijing has no DST while London and New York do, so
a naive local-time join silently misaligns by an hour for part of the year. Convert everything to
UTC at ingest. Chinese market holidays (Spring Festival, Golden Week) close SGE for multiple
consecutive days and must be an explicit unavailable state, not a forward-fill.

**EXPECTED INFORMATION GAIN:** MEDIUM-HIGH · **EXPECTED ECONOMIC VALUE:** MEDIUM
**IMPLEMENTATION COST:** MEDIUM · **RUN COST:** LOW · **PRIORITY:** P1
**RECOMMENDED STATUS:** RESEARCH_ONLY — gated on H3

## CHEAPER ALTERNATIVE

Do not build SGE order-level ingestion. Two daily published numbers plus a session-time marker
capture nearly all of the testable content. If H1 holds, the entire production cost is a calendar
entry marking two timestamps per day.

## CONFIDENCE

High on the institutional facts (they are from exchange rules). Low on economic value — H3 could
plausibly kill it, which is exactly why H3 runs first.
