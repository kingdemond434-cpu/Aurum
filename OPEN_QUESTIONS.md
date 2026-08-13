# Open questions — where contradicting Aurum is worth the most

Contributors are explicitly rewarded for falsifying anything here. Each entry states what
the desk currently believes, how strongly, and what would change its mind. If you can kill
one of these cheaply, that is worth more than a new strategy.

Last updated 2026-08-13 by the curator.

---

## Q1 — Does the desk have any selection edge at all?

**Current state:** no. Sixty-plus hypotheses tested, **zero** survive multiplicity control.
Zero promoted challengers. The desk's own signals ran −€45 against the user's +€938 over
the same twelve days.

**What would change it:** any mechanism clearing ESS ≥ 30, BH-FDR q ≤ 0.10, positive
ex-top-3, net of the 0.48 live spread.

**Most valuable contribution:** a *reason* the desk's representation is inadequate, not
another strategy to add to the pile.

---

## Q2 — Is a rendered chart read by a vision model better than engineered features?

**Current state:** unproven and currently **losing**. Head-to-head on identical historical
bars, unannotated charts, outcomes resolved strictly forward:

| competitor | acted | mean R/trade | win |
|---|---|---|---|
| vision | 5 | −0.4000 | 20% |
| numeric | 10 | −0.1000 | 30% |
| always_with | 14 | −0.1429 | 29% |
| always_fade | 14 | **+0.2397** | 43% |

n=14, ESS 7–14. Meaningless statistically, but the direction is unfavourable to vision and
a dumb always-fade control leads.

**What would change it:** a larger sample, or a better way of presenting the chart. The
reader may be handicapped by seeing one timeframe, or by the prompt.

---

## Q3 — Do chart annotations contaminate the vision read?

**Current state:** suspected yes, n=1. Same bar, two renderings:

- **annotated** → "broken major H1 support, retesting from below", `TURNING`, `AT_LEVEL`
- **clean** → "range-bound 4360–4440, no clean alignment", `CHOPPY`, mid-range

The annotated read reproduced the structural story the annotations imply. The benchmark
now scores clean charts only.

**What would change it:** a paired sample showing clean and annotated reads agree, which
would mean the annotations are informative rather than leading.

---

## Q4 — Is fading a healthy trend genuinely worse than fading a weak one?

**Current state:** believed yes, from one replay of 1,031 countertrend proposals:

| continuation health | n | mean R |
|---|---|---|
| STRONG | 383 | **−0.1429** |
| MODERATE | 337 | **+0.0638** |
| WEAK | 311 | −0.0155 |

The gate blocks STRONG only. Note the thresholds were **tuned on this same data** — the
measurement is honest, the threshold is fitted and will look worse out of sample.

**What would change it:** an out-of-sample window where STRONG fades are not the worst
cohort, or evidence that continuation health is measuring volatility rather than trend.

---

## Q5 — Is the equal-risk runner on the user's entries a real edge?

**Current state:** the only positive result the desk has ever produced (+€2,311) and it
**fails its own gate**: p=0.153, top-3 trades = 151% of the advantage, ex-top-3 negative.
Zero closed shadow pairs live.

**What would change it:** forward pairs. Nothing else.

---

## Q6 — Does gold's session structure produce a stable, exploitable effect?

**Current state:** unmeasured. Asia/London/NY, the fixes, COMEX open and rollover are all
represented in state but none has been shown to carry conditional edge on this desk.

**Most valuable contribution:** evidence that a session effect **decayed** — practitioner
folklore is full of session rules whose half-life nobody publishes.

---

## Q7 — Is macro information already priced before the desk can act?

**Current state:** assumed but never measured. The desk tracks no
`EVENT_TIME → FIRST_SEEN → REACTION_START` latency chain, so it cannot tell an insight that
is early from one that is already in the price.

**What would change it:** measured repricing latency for gold on CPI/NFP/FOMC. If gold has
fully repriced before a retail feed publishes, a whole class of planned macro work should
be abandoned — and that negative result would save more than most positive ones.

---

## Q8 — Are order-flow depth and the options surface worth paying for?

**Current state:** both marked `BLOCKED_EXTERNAL`. Assumed valuable, never demonstrated.

**Most valuable contribution:** published evidence that COMEX depth adds **no** incremental
predictive value for gold beyond price and volume. That would close a spending decision.

---

## Known gaps the desk already admits

Restating these is not new information:

- no PostgreSQL migration (SQLite is canonical, and adequate for one node)
- no live order-flow or options data
- no multi-model league beyond the single vision reader
- ICT lens is a general-knowledge reconstruction, graded E1
- 15,699 prospective decisions still pending inside their horizon
