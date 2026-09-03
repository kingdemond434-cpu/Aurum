# Backtest of every Aurum signal type — 2026-09-03

Source: `backtest_out/ledger-A-{fit,test}{0,1,2}.jsonl`, the walk-forward output already in the
repo. XAUUSD M15, **2019-03-20 → 2026-08-11**, three chronological folds, preregistered in
`backtest_out/prereg.json`.

## The answer to "use the most profitable for Telegram"

**There is no profitable signal type.** Every type loses money, in-sample and out-of-sample.

| | n | net R | mean R/trade | wins |
|---|---|---|---|---|
| **IN-SAMPLE (fit folds)** | **28** | **−16.32** | **−0.583** | **2/28** |
| SWING_REVERSAL · sweep-reclaim-trap | 22 | −10.24 | −0.466 | 2/22 |
| TREND_CONTINUATION · displacement-continuation | 6 | −6.08 | −1.013 | 0/6 |
| **OUT-OF-SAMPLE (test folds)** | **21** | **−6.63** | **−0.316** | **3/21** |
| SWING_REVERSAL · sweep-reclaim-trap | 15 | −3.02 | −0.201 | 2/15 |
| TREND_CONTINUATION · displacement-continuation | 5 | −5.11 | −1.021 | 0/5 |
| *(harness self-test row, `mechanism=test`)* | *1* | *+1.50* | — | *excluded* |

The "most profitable" type is SWING_REVERSAL at **−0.201R per trade** out of sample. Selecting it
and generating Telegram signals from it would send losing signals to subscribers, at a rate the
sample is too small to even bound properly (n=15, standard error 0.575 — the true mean could be
anywhere from −1.3R to +0.9R).

**TREND_CONTINUATION is worse than a coin flip and more interesting than that sounds.** 0 wins in
11 attempts across seven years, mean −1.02R, standard error **0.007**. Every single trade lost
almost exactly one R. A strategy with a real distribution does not do that. It means the setup
never once ran to a target, never once got managed out, never once did anything but hit full stop.
That is a signature worth investigating on its own.

## Why these numbers cannot be trusted anyway: the book is long-only

**48 signals in 7.4 years. All 48 LONG. Zero shorts.**

Gold had multiple significant downtrends in that window. A signal generator that has never once
gone short is not describing the market. Two independent causes, both confirmed in the data:

### Bug 1 — SWING_REVERSAL has no SHORT branch at all

`golddesk/runner.py`, `DeterministicAnalyst.read()`:

```python
if c.sweep_state == "CONFIRMED" and c.reclaim_state == "CONFIRMED":
    if c.trend_direction != "DOWN":
        return AnalystRead(setup=Setup.SWING_REVERSAL, direction="LONG", ...)
return none                      # <-- trend DOWN falls through to NO_SETUP
```

The mechanism is "sweep the swing low, reclaim, trap the sellers, go long". Its mirror — sweep the
swing high, reclaim, trap the buyers, go short — **is not implemented**. TREND_CONTINUATION has
both branches; SWING_REVERSAL has one.

**385 states met sweep+reclaim. 72 of them had trend DOWN and were silently discarded as
NO_SETUP.** Half of the desk's most-used mechanism has never been tested.

### Bug 2 — every short that *was* generated died on R:R

TREND_CONTINUATION does have a SHORT branch, and it fired: **42 SHORT signals were generated and
42 were refused by the compiler.** A 100% kill rate.

The reason is not inverted geometry. It is reward:risk:

```
expectancy gate: no resolved history for 'displacement-continuation';
                 R:R 0.16 vs cold-start prior 1.50
```

Observed short R:R values: 0.13, 0.16, 0.46, 0.62, 0.77 — never close to the required 1.50.

The cause is level selection. Both branches use `highs[-1]` and `lows[-1]` — the *most recent*
swing high and low, not a structurally appropriate stop and target. That produces poor geometry
generally (LONG refusals show R:R of 0.02, 0.03, 0.08, 0.10) and geometry that **never** clears the
bar for shorts. Longs squeak through occasionally; shorts never do.

So the long-only book is not one bug. It is a missing branch *and* a level-selection defect that
happens to be fatal in one direction.

## What the signal rate actually is

| | count |
|---|---|
| refusals | 4,069 |
| signals | 48 |
| **signal rate** | **1.2%** |

Refusal breakdown: `analyst: NO_SETUP` 3,585 · compiler R:R gate 484 · one-position constraint 173
· do-not-chase 6.

The desk is not being throttled by harsh gates. It is being throttled by a rule reader that finds
no setup 88% of the time and a level selector that produces unusable geometry when it does.

## What I could NOT backtest, and why

The eight quant families named in `golddesk/shadow_eval.py` — `asia_momentum`, `dow_effect`,
`failed_breakout`, `level_breakout`, `london_close_momentum`, `momentum_volgate`, `monday_gap`,
`session_range_breakout` — **could not be run**:

- **No families module in this repo.** `grep -rn "def family_"` returns nothing; `shadow_eval.py`
  takes `families_mod` as an injected parameter and it lives in the quant repo
  (`AURUM_QUANT_ROOT`), which is not attached here.
- **No bar data.** `data/` holds 24 KB — two days of tick CSVs. There is no XAUUSD history to
  backtest against. `run_backtest.py` expects `data/XAUUSD_M15.parquet`, which does not exist.

I did not reimplement those eight families. A backtest of my reimplementation would answer "how do
Claude's versions perform", not "how do Aurum's signals perform", and reporting it as the latter
would be fabrication.

## Scope limit that applies to everything above

Every one of these 48 signals came from `provider: deterministic, model: rules-v1`, with
`charts_sent: 0`. **This is arm A — the rule-based floor, not the analyst.** The ladder's LLM arms
never ran, because no API key was present. So this backtest says nothing about the discretionary
path the live desk actually uses. It measures the baseline that path is supposed to beat.

## What I did not do

I did not wire any of this into Telegram signal generation.

Picking the least-bad of two losing strategies and shipping it is not "using the most profitable
signals" — it is selecting on a backtest, which is the exact failure the desk's own promotion gate
exists to prevent, and which `state/pipeline.json` already shows happening once (in-sample Sharpe
2.99 selected from 3,168 trials with `dsr_deflated: null`).

Both types are negative. There is nothing here to promote.

## What would actually move this forward, in order

1. **Fix level selection.** `highs[-1]`/`lows[-1]` is the root cause of the 484 R:R refusals and of
   the directional asymmetry. Pick the nearest structurally valid level beyond entry, not the most
   recent swing. This is upstream of everything else.
2. **Implement the SWING_REVERSAL short branch** (done in this commit — see caveat below).
3. **Get bar data into `data/XAUUSD_M15.parquet`** so the harness can be re-run at all. `fetch_bars.py`
   and `fetch_dukascopy.py` exist for this.
4. **Attach the quant repo** so the eight families can be evaluated as themselves.
5. **Re-run with an analyst arm** so the comparison measures the desk that actually trades.
6. Only then ask which type is worth sending anywhere.

Investigate the TREND_CONTINUATION 0-for-11 at standard error 0.007 alongside step 1 — a setup that
resolves to exactly −1R every time is more likely to be a resolution bug than a bad edge.
