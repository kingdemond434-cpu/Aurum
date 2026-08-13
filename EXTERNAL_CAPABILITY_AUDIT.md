# External capability audit — what to mine, what to refuse

Nine public projects that overlap parts of Aurum. None combines the whole thing; that
integrated layer is the distinctive part. Each entry says what to take, what to leave, and
the specific test that decides.

**The discipline that matters here:** a README performance number is grade **E0**. Several
of these advertise Sharpe ratios and win rates. None is evidence until independently
reproduced on our data with our cost model. `gold_trading_system`'s own report concedes
transaction costs materially change its results — which is exactly the failure mode this
desk keeps finding in its own work.

---

## 1. World Gold Council — Goldhub · *knowledge 9/10*

Not a repo. The closest thing to an existing institutional gold brain: supply, demand,
central-bank holdings and activity, ETF flows, futures positioning, geographic premiums,
market structure, liquidity, historical regimes, plus their Gold Return Attribution Model.

**Take:** treat as a canonical source family for the macro/driver layer. GRAM is directly
relevant to §24's "what is actually driving gold right now?" — we have that question and no
attribution model to answer it.

**Test:** does a GRAM-style driver decomposition change any decision the desk makes, or
merely label what already happened? Attribution that is only explanatory is not tradeable.

**Watch:** check licensing before ingesting bulk data.

---

## 2. HKUDS / Vibe-Trading · *brain + memory 9/10*

The closest public analogue to Aurum's learning machinery: persistent research memory,
hypothesis registry with invalidation notes, hypothesis↔backtest links, run cards,
point-in-time validation, and a **Shadow Account** that analyses a broker journal.

| theirs | ours |
|---|---|
| Shadow Account | user discretionary twin |
| hypothesis registry | sealed hypotheses + promotion gate |
| run cards | `research/run_cards.py` |
| persistent memory | analogue memory + reasoning corpus |

**Take:** the hypothesis↔backtest linkage and invalidation notes. Ours records hypotheses
and results in separate places with no enforced link — theirs makes orphaned hypotheses
impossible.

**Refuse:** their strategy content. Mine the infrastructure only.

---

## 3. Open-Finance-Lab / AgenticTrading · *architecture 9/10*

Planner → orchestrator → data/alpha/risk/cost/execution agents → backtest → audit →
**Memory Agent (Neo4j + vector retrieval)**, storing execution traces, model outputs,
evaluations and feedback for continual learning.

**Take, highest value:** their *external-agent testing interface* — a model receives
timestamp-causal market snapshots, decides, and gets an hour-by-hour reasoning/trade
record. That is precisely the model-league and causal-replay harness we need, already
designed. Our `vision_benchmark.py` is a crude version of the same idea.

**Refuse for now:** Neo4j. A graph store is a real cost and SQLite plus the existing
analogue retrieval has not yet been shown insufficient. Revisit when retrieval demonstrably
fails, not before.

---

## 4. asser112 / gold_trading_system · *gold system 8/10*

Most complete gold-specific public stack: XAUUSD M5 ingestion, SQLite, 20+ features, SMC,
session/volatility features, news sentiment, XGBoost + Transformer + PPO, meta-ensemble,
backtester, live signals, MT5 EA, monitoring, Windows deployment.

**Take:** the deployment automation and monitoring plumbing. Compare its feature list
against ours and extract only variables we do not already compute.

**Refuse:** the mechanical quant-bot philosophy. Its stacked-model approach is the opposite
of the discretionary read we are pursuing, and its own cost caveat undermines the results.

---

## 5. GifariKemal / xaubot-ai · *signal engine 8/10*

37-feature XGBoost, SMC detection (order blocks, FVG, BOS, CHoCH), **three-state HMM regime
detector**, session awareness, dynamic risk, automated retraining, PostgreSQL, Telegram.

**Take:** the HMM regime detector as a **challenger** against our 11-regime softmax
classifier. Ours has never been benchmarked against an alternative — that is a real gap.

**Refuse:** fixed entry filters. We have measured that rigid thresholds destroy recall.

---

## 6. zero-was-here / tradingbot · *sensorium 8/10*

Claims 140+ features across M5–D1 plus dollar, VIX, oil, BTC and economic events; PPO and
Dreamer-style RL.

**Take:** the **feature inventory only**. Diff their variable list against ours and find
gold state variables we do not measure. That is cheap and could be genuinely additive.

**Refuse:** the advertised return targets. A README target is not production evidence.

---

## 7. andyluu98 / midas-agent · *gold LLM 8/10*

XAUUSD M15, Kronos forecasting + TradingView signals + an eight-agent DeepSeek council via
LangGraph. Unusually close to gold + LLM + charting + specialists.

**Take:** prompt and tool abstractions; worth a proper teardown.

**Refuse explicitly:** the eight-agent **voting** pattern. We already decided disagreement
is information and specialists route by competence. Averaging correlated agents manufactures
false confidence — the doctrine lenses were built specifically to avoid this.

---

## 8. shiyu-coder / Kronos · *chart intelligence 7/10*

Open-source foundation model for financial candlestick sequences. Tokenises OHLCV,
pretrained across 45+ exchanges, public weights and fine-tuning scripts.

**Take:** add as **one specialist among many**, never the CEO:

```
GOLD CEO
├── vision / chart reasoning
├── deterministic structure
├── macro
├── COMEX
├── analogue memory
├── master-trader lenses
└── KRONOS market-sequence specialist   ← orthogonal learned representation
```

**Why it is interesting:** it gives the CEO a *learned* sequence representation rather than
forcing an LLM to infer everything from raw candles. Genuinely orthogonal to both the
numeric features and the vision read.

**Test:** does adding Kronos change any decision, and does that change pay after costs?

---

## 9. JonusNattapong / Reinforcement-Learning-for-Gold-Trading · *benchmark 6/10*

PPO on 2004–2025 15-minute gold, custom environment, risk constraints, pretrained model.

**Take:** use as an **external alpha league control**, and mine its historical XAU
environment/test design. A long-history gold environment is useful infrastructure
regardless of whether its agent is any good.

**Refuse:** its reported Sharpe/win rate until reproduced.

---

## Ranked by expected marginal value to *this* desk

1. **AgenticTrading's causal snapshot harness** — we need exactly this for the model league
2. **Kronos as a specialist** — orthogonal representation, cheap to bolt on
3. **Goldhub / GRAM** — the driver-attribution layer we lack entirely
4. **Vibe-Trading's hypothesis↔backtest linkage** — closes a real gap in our registry
5. **xaubot's HMM regime detector** — first genuine challenger to our regime classifier
6. **tradingbot's feature inventory** — cheap diff, possible additive variables

## What NOT to do

Do not add these as dependencies. Clone, audit, extract mechanisms, reject duplicates, and
reimplement only what survives. Importing a stack imports its assumptions, its failure
modes and its maintenance burden — and every one of these carries performance claims that
have not been reproduced.
