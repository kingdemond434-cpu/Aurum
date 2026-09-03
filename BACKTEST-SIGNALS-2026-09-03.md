# Backtest of every Aurum signal type — 2026-09-03

> **CORRECTION, added after the first pass.** The table below counts 48 closed trades. There are
> only **23 distinct trades**. The fit folds are *expanding* windows, so fit0 ⊂ fit1 ⊂ fit2 and the
> same trade is recorded up to three times (7 trades appear once, 7 twice, 9 three times). I
> aggregated across folds without deduplicating — the exact ledger-row inflation the desk's own
> §17 warns about, and the same mistake as the live desk's 417× figure.
>
> **Deduplicated: 23 trades, −11.21R, −0.487R per trade, 21 STOP / 2 TARGET.** The conclusion does
> not change — every type still loses — but the per-type sample is roughly half what the table
> shows, so the error bars are wider still. Read the table's *signs*, not its counts.

## WHY they lose: the stop sits inside the noise

This is the root cause, and it is one number.

| | median | mean |
|---|---|---|
| **stop distance ÷ ATR** | **0.31** | 0.38 |
| target distance ÷ ATR | 1.50 | 1.96 |
| R:R at tp2 | 4.47 | 8.85 |

**The stop is 0.31 ATR from entry.** One signal's stop was 0.047 ATR — 4.99 points against an ATR
of 107. A single M15 candle in gold routinely travels further than that. The stop is not protecting
the thesis; it is sitting inside the bar-to-bar noise, so a large share of trades are stopped out
by ordinary oscillation whether the read was right or wrong.

The target, meanwhile, is 1.50 ATR away. So the geometry asks price to travel roughly **five times
further in your favour than it may travel against you, first**.

That arithmetic has a break-even:

```
break-even win rate this geometry needs : 18.3%
win rate actually achieved              :  8.7%   (2 of 23)
```

The setups are not being asked to be right. They are being asked to be right *and* never breathe.

### The mechanism, in code

```python
stop_ref=lows[-1].id,      # the MOST RECENT swing low
tp2_ref=highs[-1].id       # the MOST RECENT swing high
```

The most recent swing is, by construction, the closest one to current price — that is what makes it
most recent. Using it as the stop guarantees the tightest available stop on every trade. It looks
attractive because it maximises R:R on paper (median 4.47, mean 8.85), and the R:R gate then waves
it through for exactly that reason. **The gate is selecting for the tightest stops in the book.**

This is the failure the charter already names as a target capability — *"when a trade deserves a
wide structural stop instead of an attractive-looking tiny one"*. It is here, it is measured, and
it is currently costing every signal type its edge.

### What the excursions confirm

- **21 of 23 exits are STOP. 2 are TARGET.**
- **14 of 23 trades hit their MAE *before* their MFE** — stopped out first, so the later favourable
  move was never capturable. (An earlier draft of this file quoted "+147.5R forgone". That figure
  was wrong: most of it sits on the far side of a stop that had already been hit. Only 9 of 23 are
  genuine giveback.)
- Time-to-MFE on several trades is **71–86 days** from an M15 entry. A signal whose best outcome
  arrives three months later is not being measured on the horizon it was generated for.

## The gate admits the losing geometry and refuses the rest

| | n | R:R |
|---|---|---|
| **accepted** (became signals) | 23 | median **4.47**, **min 1.53** |
| **refused** by the expectancy gate | 104 | median **0.33**, **max 1.46** |

A clean cut at 1.50. Four out of five proposals are thrown away, and the fifth is kept *because its
R:R is high* — which, with the target fixed at the recent swing, is another way of saying **its stop
is tight**. The gate believes it is enforcing quality. What it is actually enforcing is stop
tightness, which is the single property most correlated with being stopped out by noise.

(Caveat: I could measure stop width directly only on accepted signals — refusal rows carry the R:R
in their reason string but not the compiled entry and stop. So "low R:R = wider stop" is the
reading, not a measurement. A near target would produce a low R:R too. Worth confirming by logging
compiled prices on refusals.)

This is the same signature the live desk reports independently:

```
selection :: taken trades resolve -0.13R while refusals reached +1.20R at best.
             The analyst is selecting AGAINST itself.
```

Refusals outperforming takes is exactly what a filter does when it is pointed at the wrong
property. It is not that the analyst has no good ideas. It is that the good ones have wider stops,
score worse on R:R, and are refused before they reach anyone.

## On "it shouldn't have hardcoded strategies — it should decide for itself"

Two things are worth separating, because one of them is already true.

**The strategies measured in this document are the hardcoded ones.** Every signal above came from
`provider: deterministic, model: rules-v1` — arm A, the rule-based floor. `if sweep_state ==
CONFIRMED and reclaim_state == CONFIRMED` is exactly the hardcoded logic in question, and it is
what produced −0.487R per trade. On that point the objection is correct.

**But the analyst is already free-form.** It is not choosing from a strategy list. It writes its own
`read`, `mechanism_name`, `setup`, `why` and `invalidation`, and it selects its stop and target by
citing level ids (`stop_ref: "L8"`) from the menu the brief presents. Nothing hardcodes what it may
conclude.

**What constrains it is downstream of the decision, and that is why removing hardcoded strategies
will not fix it on its own.** The analyst proposes; the same expectancy gate then applies the same
1.50 R:R threshold to whatever it proposed. An analyst that correctly reasons "this needs a wide
structural stop below the H4 swing" produces a low R:R and is refused. An analyst that picks the
nearest level produces a flattering R:R and is admitted — and then stopped out by noise.

So the live desk's `-0.125R over 37` and this document's `-0.487R over 23` are not two separate
problems with two separate causes. They are the same geometry defect reached by two different
routes: the rules reach it because they hardcode `lows[-1]`, and the analyst reaches it because the
gate rejects everything else.

Give the analyst full freedom over the signal — that is the right direction — but it will keep
producing the same losing trades until the gate stops scoring stop tightness as quality.

## The short version

Nothing here is unprofitable because the market is hard or the reads are stupid. Three compounding
defects, all in level selection:

1. **Stop too tight** (0.31 ATR) — noise stops you out before the thesis resolves.
2. **Target too far** (1.50 ATR) — needs a big move to pay for the tight stop.
3. **The R:R gate rewards both**, because tight stop ÷ far target = flattering R:R. The gate that
   was supposed to enforce quality is selecting for the exact geometry that loses.

Fix level selection and every number in this document is measured again from scratch. Until then
no signal type here can be judged on merit, because none of them has been given a survivable stop.

---


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
