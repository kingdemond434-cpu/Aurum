"""The proof loop — what the Telegram channel has actually delivered.

Every signal, refusal and close is already journalled to state/ledger.jsonl.
This module turns that journal into the one number the operator cares about —
net R, actually delivered — and the numbers that keep it honest: how much of
the total is the top trade, how the arms compare, and whether the sample is
yet large enough for any of it to mean anything.

It is DELIBERATELY deterministic: no model call, no API, no opinion. A track
record that can drift because a model summarised it loosely is not evidence.
Rendered once per UTC day by the service on rollover, and on demand with
`python -m golddesk.daily_track`.

HONESTY RULES
  * realised_r is NET of cost (the ledger's TRADE_CLOSED rows already charge
    the round trip), so "net" here is a restatement, not a claim.
  * the ex-top-3 figure is shown beside the headline because ONE trade is not
    an edge; the desk's own history says the top three trades can BE the edge.
  * n < 30 renders as INSUFFICIENT SAMPLE next to every rate, because a win
    rate over twenty trades is a mood, not a measurement.
  * shadow and live rows are never mixed: every row's arm comes from the
    entry context the trade was filed under.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

MIN_SAMPLE = 30          # the desk's own standard for a computed Sharpe
TOP_K = 3                # ex-top-K robustness figure


def _f(x) -> float:
    try:
        v = float(x)
        return v if not math.isnan(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _arm(row: dict) -> str:
    """Which arm a closed trade belongs to: vision mode, tf, shadow flag."""
    ctx = row.get("context") or {}
    bits = [str(ctx.get("tf") or "?")]
    return "/".join(bits)


def collect(rows: Iterable[dict], *, window_h: Optional[float] = None,
            since: Optional[datetime] = None) -> dict:
    """Aggregate TRADE_CLOSED rows into the report's numbers."""
    now = datetime.now(timezone.utc)
    closes = [r for r in rows if r.get("kind") == "TRADE_CLOSED"]
    if window_h is not None:
        cut = now - timedelta(hours=window_h)
        closes = [r for r in closes if _parse(r.get("ts")) and _parse(r["ts"]) >= cut]
    if since is not None:
        closes = [r for r in closes if _parse(r.get("ts")) and _parse(r["ts"]) >= since]

    rs = [_f(r.get("realised_r")) for r in closes]
    acct = [_f(r.get("account_r")) for r in closes]
    wins = [r for r in rs if r > 0]
    total = sum(rs)
    ordered = sorted(rs, reverse=True)
    ex_top = ordered[TOP_K:] if len(ordered) > TOP_K else []

    by_tf = defaultdict(lambda: {"n": 0, "r": 0.0, "wins": 0})
    by_setup = defaultdict(lambda: {"n": 0, "r": 0.0, "wins": 0})
    for r in closes:
        ctx = r.get("context") or {}
        for bucket, key in ((by_tf, ctx.get("tf") or "unknown"),
                            (by_setup, r.get("setup") or "UNKNOWN")):
            b = bucket[key]
            b["n"] += 1
            b["r"] += _f(r.get("realised_r"))
            b["wins"] += 1 if _f(r.get("realised_r")) > 0 else 0

    return {
        "n": len(closes),
        "net_r": total,
        "net_acct_r": sum(acct),
        "wins": len(wins),
        "win_rate": (len(wins) / len(rs)) if rs else None,
        "avg_r": total / len(rs) if rs else None,
        "best": ordered[0] if ordered else None,
        "worst": ordered[-1] if ordered else None,
        "ex_top_k_r": sum(ex_top),
        "ex_top_k_n": len(ex_top),
        "top_share": (sum(ordered[:TOP_K]) / total) if total > 0 and ordered else None,
        "by_tf": dict(by_tf),
        "by_setup": dict(by_setup),
        "last_ts": max((r.get("ts") for r in closes if r.get("ts")), default=None),
    }


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def render(s: dict, *, title: str = "DAILY TRACK") -> str:
    """The Telegram message. Numbers only, and only claims the sample supports."""
    if not s["n"]:
        return (f"*{title}*\nno resolved trades yet — nothing to report. "
                f"Signals and refusals are being journalled; outcomes land here "
                f"as they close.")
    wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "-"
    avg = f"{s['avg_r']:+.2f}R" if s["avg_r"] is not None else "-"
    tag = "" if s["n"] >= MIN_SAMPLE else \
        f" ⚠️ n<{MIN_SAMPLE} — INSUFFICIENT SAMPLE, rates are noise"
    lines = [
        f"*{title}*  (resolved {s['n']}){tag}",
        f"net `{s['net_r']:+.2f}R` · win `{wr}` · avg `{avg}`",
        f"best `{s['best']:+.2f}R` · worst `{s['worst']:+.2f}R`",
    ]
    if s["ex_top_k_n"]:
        lines.append(f"ex-top{TOP_K} `{s['ex_top_k_r']:+.2f}R` over "
                     f"{s['ex_top_k_n']} trades — this is the edge WITHOUT its "
                     f"three best tickets")
    if s["top_share"] is not None:
        lines.append(f"top{TOP_K} concentration `{s['top_share']:.0%}` of net")
    arms = sorted(s["by_tf"].items(), key=lambda kv: kv[1]["r"], reverse=True)
    if len(arms) > 1 or (arms and arms[0][0] not in ("unknown",)):
        lines.append("*arms:* " + " · ".join(
            f"{k} {v['r']:+.2f}R/{v['n']}" for k, v in arms[:6]))
    setups = sorted(s["by_setup"].items(), key=lambda kv: kv[1]["r"], reverse=True)
    if setups:
        lines.append("*setups:* " + " · ".join(
            f"{k} {v['r']:+.2f}R/{v['n']}" for k, v in setups[:6]))
    return "\n".join(lines)


def report_from_ledger(path: Path, *, window_h: Optional[float] = None) -> str:
    rows = []
    p = Path(path)
    if p.exists():
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return render(collect(rows, window_h=window_h))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Render the desk's track record")
    ap.add_argument("--ledger", default="state/ledger.jsonl")
    ap.add_argument("--window-hours", type=float, default=None,
                    help="limit to the last N hours (default: all time)")
    a = ap.parse_args()
    title = "LAST 24H" if a.window_hours else "TRACK RECORD"
    print(report_from_ledger(a.ledger, window_h=a.window_hours)
          .replace("*", "").replace("`", ""))
