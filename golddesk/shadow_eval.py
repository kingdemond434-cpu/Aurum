"""What each shadow cell earned yesterday. The missing half of the pipeline.

intake.run() reads `forward_returns` and nothing produced it, so the seventy-seven
candidates the hunt registered would have sat at zero fills for ever — a pipeline
that runs every morning, reports cleanly, and can never promote anything. This
closes that loop.

A CELL SPEC IS A CONTRACT AND IT HAS TO ROUND-TRIP

The hunt writes cells like

    NZDJPY|monday_gap|mode=fade,rr=2.5
    XAUUSD|session_breakout.asia|rr=2.5

and the only reason those strings are worth storing is that they can be turned
back into the exact strategy that produced them. If parsing is lossy — a dropped
parameter, a defaulted window — the forward record measures a DIFFERENT strategy
than the one that was screened, and the promotion decision is about something
nobody searched for. `parse_cell` therefore refuses rather than guesses: an
unknown family or an unparseable parameter raises, and the caller records that
the cell could not be evaluated instead of quietly evaluating the wrong thing.

FILLS, NOT ROWS

`evaluate` returns the day's summed R AND the number of fills behind it, because
the promotion floor counts trades. A day carrying six fills and a day carrying
one are not equivalent evidence, and the desk has already been bitten once by a
clock that could not tell them apart.

RESOLVED TRADES ONLY

A position opened yesterday and still open contributes NOTHING to yesterday. Its
R is not zero, it is unknown, and booking an unknown as a zero drags every
forward t-statistic toward the null — which flatters a dud and buries a real
edge. Only trades whose exit has actually happened are counted, on the day they
exited.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

SHADOW_EVAL_VERSION = "shadoweval-2026-08-18-a"


class UnparseableCell(ValueError):
    """The cell string could not be turned back into the strategy that made it.

    Raised rather than defaulted. A silently defaulted parameter means the
    forward record scores a different strategy than the one screened, and the
    promotion that follows is about something nobody searched for.
    """


def parse_cell(cell: str) -> dict:
    """`SYMBOL|family[.window]|k=v,k=v` -> {symbol, family, window, params}.

    Values are typed by trying int, then float, then leaving the string —
    families take `rr=2.5` as a float and `mode=fade` as a string, and passing
    the wrong type is the sort of thing that raises deep inside a family with a
    message about none of this.
    """
    parts = cell.split("|")
    if len(parts) < 2:
        raise UnparseableCell(f"{cell!r}: expected SYMBOL|family[|params]")
    symbol, fam = parts[0].strip(), parts[1].strip()
    window = None
    if "." in fam:
        fam, window = fam.split(".", 1)
    params: dict = {}
    if len(parts) > 2 and parts[2].strip():
        for kv in parts[2].split(","):
            if "=" not in kv:
                raise UnparseableCell(f"{cell!r}: bad parameter {kv!r}")
            k, v = kv.split("=", 1)
            k, v = k.strip(), v.strip()
            for cast in (int, float):
                try:
                    params[k] = cast(v)
                    break
                except ValueError:
                    continue
            else:
                params[k] = v
    return {"symbol": symbol, "family": fam, "window": window, "params": params}


def build_signals(spec: dict, bars, families_mod, windows: dict):
    """Rebuild the exact signal set the hunt screened.

    THE WINDOW IS PART OF THE STRATEGY. `session_breakout.asia` and
    `session_breakout.afternoon` are different cells with different results, so
    a parser that dropped the window would score one against the other's record.
    """
    fam, params = spec["family"], dict(spec["params"])
    if fam == "session_breakout":
        win = spec["window"]
        if win not in windows:
            raise UnparseableCell(f"unknown session window {win!r}")
        return families_mod.family_session_range_breakout(
            bars, **{**windows[win], **params})
    fn = {
        "asia_momentum": "family_asia_momentum",
        "dow_effect": "family_dow_effect",
        "failed_breakout": "family_failed_breakout",
        "level_breakout": "family_level_breakout",
        "london_close_mom": "family_london_close_momentum",
        "momentum_volgate": "family_momentum_volgate",
        "monday_gap": "family_monday_gap",
    }.get(fam)
    if fn is None or not hasattr(families_mod, fn):
        raise UnparseableCell(f"unknown family {fam!r}")
    return getattr(families_mod, fn)(bars, **params)


def evaluate(cells: Sequence[str], on_day: date, load_bars: Callable,
             families_mod, windows: dict, costs_for: Callable,
             run_backtest: Callable) -> tuple:
    """One day of forward evidence for every cell. Returns (returns, trades, notes).

    `load_bars(symbol)` must return bars up to and including `on_day` and NO
    FURTHER. That is the caller's responsibility and it is the whole no-lookahead
    contract: this function cannot tell whether it was handed tomorrow's data,
    and a shadow record built on future bars is worse than no shadow record
    because it looks like evidence.
    """
    returns: dict = {}
    trades: dict = {}
    notes: list = []
    cache: dict = {}
    for cell in cells:
        try:
            spec = parse_cell(cell)
        except UnparseableCell as exc:
            notes.append(f"{cell}: {exc}")
            continue
        sym = spec["symbol"]
        if sym not in cache:
            try:
                cache[sym] = load_bars(sym)
            except Exception as exc:                        # noqa: BLE001
                cache[sym] = None
                notes.append(f"{sym}: bars unavailable ({exc})")
        bars = cache[sym]
        if bars is None or len(bars) < 100:
            continue
        try:
            sigs = list(build_signals(spec, bars, families_mod, windows))
            res = run_backtest(bars, sigs, costs_for(sym))
        except Exception as exc:                            # noqa: BLE001
            notes.append(f"{cell}: evaluation failed ({exc})")
            continue
        # RESOLVED ON on_day, by EXIT time. A position still open contributes
        # nothing: its R is unknown, not zero, and booking unknowns as zeros
        # drags every forward t toward the null.
        day_r, n = 0.0, 0
        for t in res.trades:
            ex = getattr(t, "exit_time", None)
            if ex is None:
                continue
            d = ex.date() if hasattr(ex, "date") else None
            if d == on_day and math.isfinite(t.r_multiple):
                day_r += float(t.r_multiple)
                n += 1
        if n:
            returns[cell] = day_r
            trades[cell] = n
    return returns, trades, notes


def render(returns: dict, trades: dict, notes: Sequence[str],
           on_day: date) -> str:
    lines = [f"SHADOW EVALUATION  ({SHADOW_EVAL_VERSION})  {on_day}",
             f"  {len(returns)} cell(s) resolved a trade, "
             f"{sum(trades.values())} fill(s) total"]
    if returns:
        best = sorted(returns.items(), key=lambda kv: -kv[1])[:5]
        lines.append("  best today:")
        for c, r in best:
            lines.append(f"    {c:<44}{r:>+8.3f}R on {trades[c]} fill(s)")
    if notes:
        lines.append(f"  {len(notes)} cell(s) could not be evaluated:")
        for n in notes[:5]:
            lines.append(f"    {n}")
        lines.append("    NOT counted as a flat day — an unevaluable cell is "
                     "absent from the record,")
        lines.append("    because a zero it did not earn would count as "
                     "evidence it did not produce.")
    if not returns and not notes:
        lines.append("  no cell resolved a trade today. That is a normal day "
                     "for a slow book,\n  not a failure — and it correctly "
                     "advances no shadow clock.")
    return "\n".join(lines)
