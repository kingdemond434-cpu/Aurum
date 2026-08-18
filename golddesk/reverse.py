"""Reverse-engineering a copy-trade provider: where does the return come from?

The daily directive names a target — a long-lived, high-return MT5 copy system —
and asks the only question worth asking about one: can we isolate the mechanism
producing the return, strip the tail risk, and rebuild it independently with
equal or better forward geometric growth?

Everything here is built from data we are entitled to: the provider's own
published statistics, and our OWN mirrored fills from a subscription we pay for.
Nothing in this module fetches anything; it takes rows and analyses them, so the
question of what is lawful to collect stays where it belongs — with whoever
collects it — and never gets answered implicitly by an import.

THE FINDING THIS MODULE EXISTS TO PRODUCE, AND WHY IT IS USUALLY THE SAME ONE

A very large fraction of high-return, long-lived retail copy systems are not
selling directional skill. They are selling INSURANCE: a grid or martingale that
adds to losers until the market retraces, which converts a low-variance stream
of small wins into a rare, total loss. The equity curve is beautiful precisely
because the risk has been moved into a tail that has not arrived yet, and the
longer the record, the more convincing the curve and the closer the ruin.

That is why `ruin_forensics()` matters more than any return statistic here. A
track record cannot show you the drawdown that ends the account, because if it
had, the account would be gone and the provider delisted. Survivorship is not a
caveat on this analysis — it is the mechanism generating the thing being
analysed.

So the decisive number is `recovery_free_return`: what the record would have
paid with the recovery layer removed — first entry only, fixed size. If that is
negative, the provider has no entry edge at all and the entire return is the
short-volatility position. Copying it means inheriting a bet whose expected
value depends on a retracement that is not promised.

WHAT "IMPROVE, DO NOT CLONE" MEANS OPERATIONALLY

If the third entry after two failures has the real expectancy, the honest
strategy is to trade that third state DIRECTLY, on its own entry criteria,
without first taking the two losing positions that produced it. `ablate()`
produces exactly those variants so the question is settled by measurement.

NOTHING HERE PROMOTES ANYTHING

A reconstructed strategy is a hypothesis, and it enters `linkage.py` as a run
against a registered claim like every other trial. That a provider made money is
not evidence that a mechanism works; it is the reason to go looking.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

REVERSE_VERSION = "reverse-2026-08-18-a"

#: Trades in the same symbol and direction opening within this window of an
#: existing open position are treated as ONE basket. Copy systems add over
#: minutes to hours; a gap larger than this is a new decision, not an add.
BASKET_WINDOW = timedelta(hours=12)

#: Below this many baskets, no structural claim is made. A grid's character is a
#: property of its distribution, and a handful of baskets shows none of it.
MIN_BASKETS = 20

#: Lot ratio between consecutive adds at or above which escalation is called
#: progressive rather than flat.
ESCALATION_FLAT = 1.15


@dataclass(frozen=True)
class Trade:
    """One mirrored fill. The fields a copy feed actually gives you."""
    ticket: str
    symbol: str
    direction: str                       # BUY | SELL
    lots: float
    open_utc: datetime
    close_utc: Optional[datetime]
    open_price: float
    close_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    profit: Optional[float] = None
    #: Worst unrealised excursion while open, if the feed reports it. Absent for
    #: most public statements, which is itself a finding — see `mae_coverage`.
    mae_price: Optional[float] = None

    @property
    def sign(self) -> int:
        return 1 if self.direction.upper().startswith("B") else -1

    @property
    def closed(self) -> bool:
        return self.close_utc is not None and self.close_price is not None

    def pnl_price(self) -> Optional[float]:
        """Signed price movement captured. Currency-free, so it compares across
        account sizes and lot conventions."""
        if not self.closed:
            return None
        return (self.close_price - self.open_price) * self.sign


@dataclass
class Basket:
    """Trades the provider was managing as one position."""
    symbol: str
    direction: str
    trades: list = field(default_factory=list)

    @property
    def depth(self) -> int:
        return len(self.trades)

    @property
    def opened(self) -> datetime:
        return min(t.open_utc for t in self.trades)

    @property
    def closed_at(self) -> Optional[datetime]:
        if any(not t.closed for t in self.trades):
            return None
        return max(t.close_utc for t in self.trades)

    @property
    def total_lots(self) -> float:
        return sum(t.lots for t in self.trades)

    @property
    def first(self) -> Trade:
        return min(self.trades, key=lambda t: t.open_utc)

    def ordered(self) -> list:
        return sorted(self.trades, key=lambda t: t.open_utc)

    @property
    def profit(self) -> Optional[float]:
        vals = [t.profit for t in self.trades if t.profit is not None]
        return sum(vals) if len(vals) == len(self.trades) else None

    def escalation(self) -> Optional[float]:
        """Geometric mean lot ratio between consecutive adds.

        1.0 is flat sizing. 2.0 is a classic martingale. Between them is the
        interesting territory, and the number tells you how fast exposure grows
        with adversity — which is the same as how fast ruin approaches.
        """
        o = self.ordered()
        if len(o) < 2:
            return None
        ratios = [o[i + 1].lots / o[i].lots for i in range(len(o) - 1)
                  if o[i].lots > 0]
        if not ratios:
            return None
        return math.exp(sum(math.log(r) for r in ratios) / len(ratios))

    def common_exit(self, tolerance_s: int = 120) -> bool:
        """Did every leg close together? The signature of a basket TP.

        A common exit means the provider manages total basket P&L rather than
        each trade's own thesis, which is the defining behaviour of a recovery
        system and completely changes what a per-trade win rate means.
        """
        if any(not t.closed for t in self.trades) or len(self.trades) < 2:
            return False
        times = [t.close_utc for t in self.trades]
        return (max(times) - min(times)).total_seconds() <= tolerance_s

    def adverse_before_add(self) -> list:
        """For each add, the price movement against the basket since it opened.

        THE DISCRIMINATOR BETWEEN SCALING AND RECOVERY. A provider who adds only
        when underwater is running a martingale; one who adds regardless of P&L
        is pyramiding on signal. These have opposite risk profiles and identical
        equity curves right up until they do not.
        """
        o = self.ordered()
        if len(o) < 2:
            return []
        base, s = o[0].open_price, o[0].sign
        return [(t.open_price - base) * s for t in o[1:]]


def build_baskets(trades: Iterable[Trade],
                  window: timedelta = BASKET_WINDOW) -> list:
    """Group fills into the positions the provider was actually managing.

    Grouped by symbol AND direction: a hedge is not an add, and folding opposite
    sides into one basket would report a martingale as flat sizing.
    """
    by_key: dict = defaultdict(list)
    for t in trades:
        by_key[(t.symbol, t.direction.upper()[:1])].append(t)
    out: list = []
    for (sym, side), group in by_key.items():
        group.sort(key=lambda t: t.open_utc)
        cur: list = []
        for t in group:
            if cur and (t.open_utc - cur[-1].open_utc) <= window:
                cur.append(t)
            else:
                if cur:
                    out.append(Basket(sym, cur[0].direction, cur))
                cur = [t]
        if cur:
            out.append(Basket(sym, cur[0].direction, cur))
    return sorted(out, key=lambda b: b.opened)


# ------------------------------------------------------------ structure

@dataclass
class Structure:
    """What kind of machine is this?"""
    n_baskets: int
    max_depth: int
    mean_depth: float
    escalation: Optional[float]
    common_exit_rate: float
    add_when_losing_rate: Optional[float]
    kind: str                    # RECOVERY_GRID | SIGNAL_PYRAMID | SINGLE_ENTRY | UNKNOWN
    confidence: str              # LOW | MEDIUM | HIGH
    why: str

    def render(self) -> str:
        esc = "n/a" if self.escalation is None else f"{self.escalation:.2f}x"
        awl = ("n/a" if self.add_when_losing_rate is None
               else f"{self.add_when_losing_rate:.0%}")
        return "\n".join([
            f"STRUCTURE — {self.kind}  (confidence {self.confidence})",
            f"  baskets              {self.n_baskets}",
            f"  depth                max {self.max_depth}, mean {self.mean_depth:.2f}",
            f"  lot escalation       {esc}",
            f"  common basket exit   {self.common_exit_rate:.0%}",
            f"  adds while losing    {awl}",
            f"  {self.why}",
        ])


def infer_structure(baskets: Sequence[Basket]) -> Structure:
    """Classify the machine from its own behaviour, not from its description."""
    n = len(baskets)
    if n == 0:
        return Structure(0, 0, 0.0, None, 0.0, None, "UNKNOWN", "LOW",
                         "no baskets supplied")
    depths = [b.depth for b in baskets]
    multi = [b for b in baskets if b.depth >= 2]
    escs = [e for e in (b.escalation() for b in multi) if e is not None]
    esc = statistics.median(escs) if escs else None
    ce = (sum(1 for b in multi if b.common_exit()) / len(multi)) if multi else 0.0

    adverse = [d for b in multi for d in b.adverse_before_add()]
    awl = (sum(1 for d in adverse if d < 0) / len(adverse)) if adverse else None

    conf = "HIGH" if n >= MIN_BASKETS * 3 else "MEDIUM" if n >= MIN_BASKETS else "LOW"
    if not multi:
        return Structure(n, max(depths), statistics.mean(depths), None, 0.0, None,
                         "SINGLE_ENTRY", conf,
                         "every basket is one trade: no adds, no grid, no recovery "
                         "layer. Whatever the return is, it is entry and exit.")
    if awl is not None and awl >= 0.8:
        kind = "RECOVERY_GRID"
        why = (f"{awl:.0%} of adds happen while the basket is UNDERWATER. This is "
               f"a recovery machine: it converts small frequent wins into a rare "
               f"total loss, and the track record cannot contain the loss that "
               f"ends it. Treat the headline return as a premium collected, not "
               f"an edge earned.")
    elif awl is not None and awl <= 0.4:
        kind = "SIGNAL_PYRAMID"
        why = (f"only {awl:.0%} of adds happen while underwater, so adds are "
               f"conditioned on something other than adversity — most likely a "
               f"signal. This has a genuinely different risk profile from a grid "
               f"and its adds may carry real information.")
    else:
        kind = "UNKNOWN"
        why = (f"{awl:.0%} of adds happen while underwater — between the grid and "
               f"pyramid signatures. More baskets, or the entry timestamps at "
               f"finer resolution, would separate them.")
    if conf == "LOW":
        why += (f" ONLY {n} BASKETS: this is a description of a small sample, not "
                f"a property of the system. {MIN_BASKETS} is the floor for a "
                f"structural claim.")
    return Structure(n, max(depths), statistics.mean(depths), esc, ce, awl,
                     kind, conf, why)


# --------------------------------------------------------- tail-risk forensics

@dataclass
class RuinForensics:
    """How close did it come, and what is the shape of the end?"""
    max_depth: int
    max_basket_lots: float
    escalation: Optional[float]
    worst_adverse_price: Optional[float]
    mae_coverage: float
    exposure_at_max_depth: float
    depth_headroom: Optional[int]
    survived_by: Optional[str]
    verdict: str

    def render(self) -> str:
        lines = [
            "TAIL-RISK FORENSICS",
            f"  deepest basket seen      {self.max_depth} legs, "
            f"{self.max_basket_lots:.2f} lots total",
            f"  lot escalation           "
            + ("n/a" if self.escalation is None else f"{self.escalation:.2f}x per add"),
            f"  worst adverse excursion  "
            + ("UNREPORTED" if self.worst_adverse_price is None
               else f"{self.worst_adverse_price:.2f} price units"),
            f"  MAE coverage             {self.mae_coverage:.0%} of trades",
        ]
        if self.depth_headroom is not None:
            lines.append(f"  depth headroom           {self.depth_headroom} more "
                         f"add(s) before exposure doubles again")
        if self.survived_by:
            lines.append(f"  {self.survived_by}")
        lines.append(f"  {self.verdict}")
        return "\n".join(lines)


def ruin_forensics(baskets: Sequence[Basket], structure: Structure,
                   equity: Optional[float] = None) -> RuinForensics:
    """What the track record cannot show you.

    A record cannot contain the drawdown that ends the account, because if it
    had, the account would be gone and the provider delisted. Survivorship is
    not a caveat here — it is the mechanism generating the record. So the
    question is never "what was the worst drawdown" but "how much further could
    it have gone before the end", and that is a function of the escalation
    ladder, not of the observed history.
    """
    if not baskets:
        return RuinForensics(0, 0.0, None, None, 0.0, 0.0, None, None,
                             "no baskets to examine")
    deepest = max(baskets, key=lambda b: b.depth)
    all_t = [t for b in baskets for t in b.trades]
    with_mae = [t for t in all_t if t.mae_price is not None]
    cov = len(with_mae) / len(all_t) if all_t else 0.0
    worst = (max(abs(t.mae_price) for t in with_mae) if with_mae else None)

    esc = structure.escalation
    lots = deepest.total_lots
    headroom = None
    survived = None
    if esc and esc > 1.0:
        # How many more adds before total exposure doubles? The number that says
        # how fast the end arrives once it starts, and it is small for any
        # escalation worth calling a martingale.
        headroom = max(1, int(math.ceil(math.log(2.0) / math.log(esc))))
        if equity and worst:
            # Adverse movement already absorbed at the deepest basket, against
            # what one more doubling would cost.
            absorbed = lots * worst
            survived = (f"the deepest basket had {lots:.2f} lots against a "
                        f"{worst:.2f} adverse move — roughly {absorbed:,.0f} in "
                        f"exposure-price units on an equity of {equity:,.0f}. "
                        f"{headroom} more add(s) doubles that.")

    if structure.kind == "RECOVERY_GRID":
        verdict = (
            "DO NOT INHERIT THIS UNMODIFIED. The return is compensation for "
            "carrying a tail the record cannot show. Take the entry criteria if "
            "they survive ablation; leave the recovery layer, which is where the "
            "ruin lives.")
    elif structure.kind == "SINGLE_ENTRY":
        verdict = ("no basket layer, so no hidden recovery risk. Whatever this "
                   "earns, it earns from entries and exits and can be judged "
                   "on ordinary terms.")
    else:
        verdict = ("structure not established well enough for a tail verdict. "
                   "Absence of an observed catastrophe is not evidence of its "
                   "impossibility.")
    if cov < 0.5:
        verdict += (f" MAE is reported for only {cov:.0%} of trades, so the worst "
                    f"excursion above is a LOWER BOUND on what actually happened.")
    return RuinForensics(deepest.depth, lots, esc, worst, cov,
                         lots, headroom, survived, verdict)


# ------------------------------------------------------------------ ablation

@dataclass
class Ablation:
    name: str
    n_trades: int
    total_price: float
    mean_price: float
    win_rate: float
    worst: float
    why: str = ""

    def render(self) -> str:
        return (f"  {self.name:<28} n={self.n_trades:<5} total="
                f"{self.total_price:>9.1f}  mean={self.mean_price:>7.3f}  "
                f"win={self.win_rate:>4.0%}  worst={self.worst:>8.2f}")


def _score(name: str, trades: Sequence[Trade], why: str = "") -> Ablation:
    """Scored in PRICE units, not currency.

    Currency folds the sizing decision into the entry measurement, which is the
    one thing an ablation must keep apart: a martingale's currency P&L is
    dominated by the biggest leg, so a currency-scored 'first entry only' arm
    would look terrible for reasons that have nothing to do with the entry.
    """
    vals = [t.pnl_price() for t in trades]
    vals = [v for v in vals if v is not None]
    if not vals:
        return Ablation(name, 0, 0.0, 0.0, 0.0, 0.0, why or "no closed trades")
    wins = [v for v in vals if v > 0]
    return Ablation(name, len(vals), sum(vals), sum(vals) / len(vals),
                    len(wins) / len(vals), min(vals), why)


def ablate(baskets: Sequence[Basket]) -> dict:
    """Where does the return come from? Answered by removing one layer at a time.

    `recovery_free` is the decisive arm. It is the record with the recovery layer
    gone — first entry of each basket only, at flat size. If it is negative, the
    provider has NO entry edge and the whole return is the short-volatility
    position; copying it means inheriting a bet on a retracement nobody promised.
    """
    all_t = [t for b in baskets for t in b.trades]
    firsts = [b.first for b in baskets]
    later = [t for b in baskets for t in b.ordered()[1:]]
    thirds = [b.ordered()[2] for b in baskets if b.depth >= 3]
    singles = [b.first for b in baskets if b.depth == 1]
    deep = [t for b in baskets if b.depth >= 3 for t in b.trades]

    arms = [
        _score("original (all fills)", all_t,
               "the record as traded, in price units"),
        _score("first entry only", firsts,
               "THE DECISIVE ARM: the entry signal with no recovery layer"),
        _score("later entries only", later,
               "what the adds contribute on their own"),
        _score("third entry only", thirds,
               "if this is the good one, trade IT directly rather than "
               "inheriting the two losses that produce it"),
        _score("baskets that never added", singles,
               "entries that worked first time: the cleanest read on the signal"),
        _score("deep baskets (3+)", deep,
               "where the tail lives"),
    ]
    by_name = {a.name: a for a in arms}
    rec_free = by_name["first entry only"]
    original = by_name["original (all fills)"]

    if rec_free.n_trades == 0:
        verdict = "nothing closed; no decomposition possible"
    elif rec_free.mean_price <= 0 < original.mean_price:
        verdict = (
            "THE RETURN IS THE RECOVERY LAYER. Entries alone lose in price terms; "
            "the record is positive only because losers are held and added to "
            "until they come back. There is no entry edge here to extract, and "
            "copying this is taking the other side of a rare, total loss.")
    elif rec_free.mean_price > 0:
        verdict = (
            f"THERE IS AN ENTRY EDGE. First entries alone average "
            f"{rec_free.mean_price:+.3f} price units over {rec_free.n_trades} "
            f"baskets. That is the part worth rebuilding — independently, at "
            f"bounded risk, without the basket layer.")
    else:
        verdict = ("neither the entries nor the record is positive in price "
                   "terms; there is nothing here to reverse-engineer.")

    return {
        "version": REVERSE_VERSION,
        "arms": arms,
        "recovery_free_mean": rec_free.mean_price,
        "original_mean": original.mean_price,
        "verdict": verdict,
        "note": ("Scored in PRICE units, never currency. Currency folds the "
                 "sizing decision into the entry measurement — a martingale's "
                 "currency P&L is dominated by its largest leg, so a "
                 "currency-scored 'first entry only' arm looks terrible for "
                 "reasons unrelated to the entry."),
    }


# -------------------------------------------------------- behavioural replication

@dataclass
class Replication:
    n: int
    direction_match: float
    timing_error_median_s: Optional[float]
    depth_match: float
    why: str

    def render(self) -> str:
        t = ("n/a" if self.timing_error_median_s is None
             else f"{self.timing_error_median_s / 60:.1f} min")
        return (f"REPLICATION  n={self.n}\n"
                f"  direction match      {self.direction_match:.0%}\n"
                f"  median timing error  {t}\n"
                f"  basket depth match   {self.depth_match:.0%}\n"
                f"  {self.why}")


def replicate(baskets: Sequence[Basket], predict) -> Replication:
    """Score a candidate model against what the provider actually did.

    `predict(basket_open_utc, symbol, history)` receives ONLY the baskets that
    closed before the one being predicted. Passing the full list would let a
    model fit the future, and a replication score is exactly the number that
    would look spectacular if it did.
    """
    ordered = sorted(baskets, key=lambda b: b.opened)
    dir_hits, depth_hits, errs, n = 0, 0, [], 0
    for i, b in enumerate(ordered):
        history = [h for h in ordered[:i] if h.closed_at and h.closed_at <= b.opened]
        try:
            pred = predict(b.opened, b.symbol, history)
        except Exception:                            # noqa: BLE001
            continue
        if not pred:
            continue
        n += 1
        if str(pred.get("direction", "")).upper()[:1] == b.direction.upper()[:1]:
            dir_hits += 1
        if pred.get("depth") is not None and int(pred["depth"]) == b.depth:
            depth_hits += 1
        if pred.get("open_utc") is not None:
            errs.append(abs((pred["open_utc"] - b.opened).total_seconds()))
    if n == 0:
        return Replication(0, 0.0, None, 0.0,
                           "the model predicted nothing on any basket")
    dm = dir_hits / n
    return Replication(
        n, dm, statistics.median(errs) if errs else None, depth_hits / n,
        ("direction match near 50% is a coin flip, not a reconstruction — on a "
         "two-sided market it is what a model that has learned nothing scores."
         if 0.4 <= dm <= 0.6 else
         "a direction match well away from 50% means the model has found "
         "something about WHEN this provider acts. It says nothing yet about "
         "whether acting then is profitable."))


def report(trades: Sequence[Trade], equity: Optional[float] = None) -> str:
    """One call: structure, tail risk, decomposition."""
    baskets = build_baskets(trades)
    st = infer_structure(baskets)
    rf = ruin_forensics(baskets, st, equity)
    ab = ablate(baskets)
    lines = [f"REVERSE-ENGINEERING REPORT  ({REVERSE_VERSION})",
             f"  {len(trades)} fills -> {len(baskets)} baskets", "",
             st.render(), "", rf.render(), "", "DECOMPOSITION"]
    lines += [a.render() for a in ab["arms"]]
    lines += ["", f"  {ab['verdict']}", "", f"  {ab['note']}", "",
              "  Nothing here promotes anything. A reconstructed strategy is a "
              "hypothesis and enters the registry as a run against a registered "
              "claim, like every other trial."]
    return "\n".join(lines)
