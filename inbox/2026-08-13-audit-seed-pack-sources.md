---
title: Audit of the external Gold seed pack — nine sources verified for reachability, ownership and licence
source: direct fetch of each source URL, 2026-08-13
source_type: ai_analysis
language: en
evidence_grade: E1
claim: All eight GitHub sources in the seed pack resolve, but one has moved owner, one carries no licence at all and is therefore not legally reusable, and the seed pack's own priority-1 source is unreachable from this environment.
mechanism: An audit trail that records provenance, licence and reachability before extraction prevents the desk spending research effort on links that do not resolve, and prevents copying code that cannot legally be used.
conditions: Applies to the seed pack note dropped 2026-08-13; re-verify before any extraction work, since repository ownership, licence and availability all change without notice.
anti_conditions: This audit is stale the moment any source is renamed, relicensed, deleted or made private; star counts and commit counts were read off rendered pages and were not independently confirmed against the GitHub API.
---

# Audit — external Gold seed pack sources

Verification performed by direct HTTP fetch of each URL on 2026-08-13. This audit records
**provenance and legal status only**. Nothing here says any source has trading edge.

## Result table

| # | Source | Resolves | Licence | Notes |
|---|--------|----------|---------|-------|
| 1 | gold.org / Goldhub | **NO — blocked** | n/a | egress proxy blocks `www.gold.org` from this environment |
| 2 | HKUDS/Vibe-Trading | yes | MIT | reusable with attribution |
| 3 | Open-Finance-Lab/AgenticTrading | yes | **OpenMDW-1.0** | not a standard OSI code licence — read before reuse |
| 4 | asser112/gold_trading_system | yes | **NONE DECLARED** | all rights reserved by default — read-only study, do not copy |
| 5 | GifariKemal/xaubot-ai | yes | MIT | reusable with attribution |
| 6 | forbbiden403/tradingbot | **redirects** | MIT | now `zero-was-here/tradingbot` — owner renamed |
| 7 | andyluu98/midas-agent | yes | Apache-2.0 | reusable; patent grant included |
| 8 | shiyu-coder/Kronos | yes | MIT | model weights may carry separate terms — check the model card |
| 9 | JonusNattapong/RL-for-Gold-Trading | yes | MIT | reusable with attribution |

## Corrections to the seed pack

1. **`forbbiden403/tradingbot` no longer exists under that owner.** It resolves by redirect to
   `zero-was-here/tradingbot`. GitHub redirects break when the old username is re-registered by
   someone else, so the seed pack's URL is not a durable reference. Pin the new path.
2. **`asser112/gold_trading_system` has no licence file.** Under GitHub's terms, absence of a
   licence means default copyright — no reuse rights beyond viewing and forking within GitHub.
   The seed pack lists it for "audit … data ingestion, features, SMC … deployment patterns."
   Reading it for *ideas* is fine; copying any of its code is not. Move it to study-only.
3. **`Open-Finance-Lab/AgenticTrading` is OpenMDW-1.0**, an Open Model, Data and Weights licence,
   not the permissive code licence the seed pack's "legally reusable implementation patterns"
   line assumes. Its obligations differ from MIT/Apache. Legal read required before extraction.
4. **`andyluu98/midas-agent` is a Vietnamese-language project** describing an M15 XAUUSD scalping
   framework with an "8-agent DeepSeek council". Seven stars, four forks. The seed pack rates it
   #6 in priority audit order; its scale does not support that. Its only distinctive content is
   the Kronos + TradingView tool wiring, which is better read directly from Kronos.

## Operational finding — egress

Three domains needed for this research are **blocked by the network egress proxy** in this
environment:

- `www.gold.org` — the seed pack's **priority-1** source
- `papers.ssrn.com`
- `www.ecb.europa.eu`

This is not a transient failure and no proxy setting should be changed to work around it. Two
consequences the desk should plan for:

- The seed pack's stated audit order begins with a source that cannot be fetched from here at
  all. Either the ingestion runs from an environment with different egress, or Goldhub data is
  obtained via a mirror the desk controls. **Priority order must be rewritten to lead with
  something reachable.**
- Any scheduled research job that assumes these domains are available will fail silently or
  produce an empty result that looks like "no news". Feed adapters need an explicit
  `SOURCE_UNREACHABLE` state distinct from `NO_DATA`, or absence of evidence gets recorded as
  evidence of absence.

## What was not verified

Star counts, commit counts and "last updated" dates were read from rendered pages by a
summarising model and are **not** confirmed. They are not load-bearing for any decision here.
No repository's *claimed performance* was examined, reproduced, or is treated as evidence.
