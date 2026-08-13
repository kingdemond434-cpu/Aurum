# Architecture decisions

Standing decisions with the reasoning attached, so they are not silently reversed later by
someone who only sees the conclusion. Each records what was decided, why, and what evidence
would overturn it.

---

## AD-1 — Aurum stays a single-market specialist. BTC becomes a sibling desk.

**Decided 2026-08-13.**

The temptation is to add BTC so there is something to trade at weekends. Rejected as
specified, accepted in a different shape.

**Why merging is wrong:** the entire thesis of this desk is compounding specialisation.
Every bar, signal, missed setup, stop movement, macro event and failed hypothesis enriches
*one* domain model. Adding a second market inside the same brain means a BTC lesson —
"weekend liquidity sweeps behave like X" — can silently influence an XAU decision where the
microstructure is unrelated. That is not diversification, it is contamination.

**Why frequency is the wrong reason:** adding an instrument to guarantee weekend activity
makes trade count the objective. This desk has spent its entire existence learning that
frequency is not the objective and that silence is a valid output.

**The shape instead:**

```
AURUM PLATFORM
│
├── AURUM GOLD DESK                    ← the specialist, unchanged
│   ├── Gold CEO, Gold memory
│   ├── COMEX / macro / USD / yields
│   ├── Gold visual corpus
│   └── Gold research factory
│
└── BTC WEEKEND DESK                   ← sibling, never a tenant
    ├── BTC CEO, BTC memory
    ├── crypto-native drivers
    └── BTC research factory
```

**Shared:** telemetry, database primitives, model routing, replay engine, evidence
framework, Telegram, compiler safety, monitoring, promotion gate. All the boring parts.

**Never shared:** market memory, visual corpus, regime models, macro drivers, execution
assumptions, trade-management statistics, analogues, promotion evidence.

A thin supervisor may later compare opportunity quality across desks. Neither desk teaches
the other unless a cross-market hypothesis is *explicitly* stated and tested.

**Not now.** BTC earns a desk when there is evidence of enough worthwhile weekend
opportunity to justify one — not because the calendar has a gap. Gold has no validated edge
yet; building a second desk first would be avoidance.

---

## AD-2 — Specialists route by competence. No majority voting. Ever.

**Standing, reaffirmed 2026-08-13** against the eight-agent-council pattern in
`midas-agent`.

Averaging correlated agents manufactures confidence that the underlying evidence does not
support. When six doctrine lenses agree, that may be one idea counted six times — the
lens module says so in its own output. Disagreement is information about regime ambiguity,
not a tie to be broken.

**Overturned by:** evidence that a voted ensemble beats competence routing on identical
frozen packets. Not by it being the common pattern.

---

## AD-3 — Learned models are specialists, never the CEO.

Kronos, XGBoost, HMM regime detectors, RL agents: each may join as one voice with a stated
competence and abstention behaviour. None becomes the decision layer.

**Why:** a model that cannot say "not my regime" and cannot expose its reasoning cannot be
audited, and this desk's entire failure history is confident outputs that nobody could
check. The CEO must remain something whose reasoning is inspectable.

---

## AD-4 — The objective, stated so it cannot be gamed.

Not *"prove Aurum is the world's best gold trader."* That framing invites overfitting and
self-deception, and it is unfalsifiable.

> **Continuously maximise independently verified net trading value from XAUUSD by becoming
> increasingly specialised in every economically relevant aspect of the gold market, while
> aggressively discovering positive-EV opportunities and continuously falsifying its own
> beliefs.**

Three load-bearing words: **independently verified** (not self-reported), **net** (after
the live spread), **falsifying** (the desk must attack itself).

If years of forward results show it beats retail gold systems, signal providers and public
benchmarks at comparable risk, the evidence earns that claim. The claim is never made in
advance.

---

## AD-5 — Gold-specific knowledge targets

What deep specialisation should eventually mean concretely. These are **targets, not
capabilities** — none is currently encoded, and listing them is not claiming them:

- a genuine liquidation versus an ordinary pullback
- London→NY handoff behaviour under different macro conditions
- XAU response to a yield move conditional on whether USD confirms it
- when an M5 reversal is only a retracement inside H1 displacement
- ordinary XAU wick noise by time of day
- which failed continuations produce asymmetric reversals
- gold behaviour 0–30 versus 30–120 minutes after CPI/PPI/NFP/FOMC
- when COMEX futures meaningfully lead retail XAUUSD
- when a trade deserves a wide structural stop over an attractive tiny one
- when to monetise MFE versus preserve an unusually valuable right tail

Each is a testable hypothesis. Each needs data the desk does not fully have. This list is a
research frontier, not an inventory.

---

## AD-6 — The telemetry loop is the point.

"That trade won" is nearly worthless. The loop worth building records: what the chart
looked like at entry → what the trader saw → where the stop was → **every** stop movement →
what price did next → what could have been banked → whether the runner decision was
rational → whether this visual state has occurred before.

Status: partial. Chart rendering at any historical bar works. Stop-movement history and
visual-state matching do not exist yet.
