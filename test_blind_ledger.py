"""A bar the analyst never answered on must leave a row.

THE DEFECT THESE GUARD. Every `except AnalystError` in `live.py` logged a
warning and `return`ed. No ledger row. The consequence is not a thin log — it
is that `state/ledger.jsonl`, which is the ONLY artifact every downstream
measurement reads, could not distinguish:

    a session the desk spent DECLINING   (the analyst read, and said no)
    a session the desk spent BLIND       (the analyst never answered)

Three ledger rows over a live window reads as a disciplined desk seeing nothing
worth taking. It was in fact a provider timing out. Those are opposite facts
about whether the desk works, and they had the same file — absence read as a
clean answer (WS-005 / L1.28a).

The fix journals a BLIND row. The name is load-bearing and is guarded here:
`missed_money.price_restrictions` and `constitution` select refusals with
`.startswith("REFUSAL")` and charge the forward move to whatever declined.
Nothing declined on a blind bar, so filing one as a refusal would bill a gate
that never ran AND let an outage read as discipline.

    python3 -m pytest test_blind_ledger.py -q
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import Thresholds
from golddesk.features import Bar, atr, classify, swings
from golddesk.ledger import DecisionKind, Ledger
from golddesk.live import LiveDesk, Vision
from golddesk.notify import Sink
from golddesk.providers import AnalystError, AnalystProvider

UTC = timezone.utc
LIVE_PY = Path(__file__).parent / "golddesk" / "live.py"


class NullSink(Sink):
    def send(self, text):
        return True


class BlindProvider(AnalystProvider):
    """The provider that never answers. This is the failure actually observed
    in production: a 240s timeout, raised as AnalystError, on every wake."""
    name, model = "blind", "blind-v1"

    def __init__(self):
        self.calls = 0

    def read(self, brief, charts=()):
        self.calls += 1
        raise AnalystError("simulated timeout — the analyst never answered")

    def survey(self, brief, charts=()):
        self.calls += 1
        raise AnalystError("simulated timeout — the analyst never answered")


def _bars(n=460):
    """Structure at TWO scales plus jitter — borrowed from universe_check.py.

    A perfectly regular path makes the turning bar's high TIE with its
    neighbour's; the fractal swing test needs a STRICT local extreme, so a clean
    zigzag yields zero swings, zero structure, zero decisions and a green run
    that proved nothing. The jitter is load-bearing, not decoration.
    """
    import math
    now = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
    seed = 20260814

    def jitter() -> float:
        nonlocal seed
        seed = (1103515245 * seed + 12345) % (1 << 31)
        return (seed / (1 << 31) - 0.5) * 1.6

    out, prev = [], 2000.0
    for k in range(n):
        px = (2000.0 + 26.0 * math.sin(2 * math.pi * k / 41)
              + 13.0 * math.sin(2 * math.pi * k / 13) + jitter())
        h = max(prev, px) + 0.5 + abs(jitter())
        lo = min(prev, px) - 0.5 - abs(jitter())
        out.append(Bar(now + timedelta(minutes=15 * k), prev, h, lo, px))
        prev = px
    return out


def _drive(tmp_path, universe_mode=False):
    """Drive real bars through the real LiveDesk against a provider that only
    ever raises. Returns the ledger rows written."""
    bars = _bars()
    atrs, sw = atr(bars), swings(bars)
    out = tmp_path / "l.jsonl"
    desk = LiveDesk(BlindProvider(), Ledger(out), NullSink(),
                    shadow=True, vision=Vision.NUMERIC_ONLY,
                    thresholds=Thresholds(fallback_min_rr=1.0),
                    universe_mode=universe_mode,
                    measure_position_constraint=False)
    tl: list[str] = []
    for i in range(60, len(bars) - 61):
        st = classify(bars, i, sw, atrs)
        if st is None:
            continue
        tl.append(f"{bars[i].ts.date()} {st.trend_direction}/{st.trend_health}")
        tl[:] = tl[-8:]
        desk.on_bar(bars, i, sw, atrs, None,
                    (bars[i].close - 0.05, bars[i].close + 0.05, 1.0), tl)
    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()
            if ln.strip()] if out.exists() else []
    return desk, rows


# ------------------------------------------------------------------ the rows

def test_a_bar_the_analyst_never_answered_leaves_a_row(tmp_path):
    desk, rows = _drive(tmp_path)
    assert desk.stats.analyst_errors > 0, "the harness never reached the analyst"
    blind = [r for r in rows if r.get("kind") == "BLIND"]
    assert len(blind) == desk.stats.analyst_errors, (
        f"{desk.stats.analyst_errors} failed reads produced {len(blind)} rows — "
        f"a blind bar vanished, which is the whole defect")


def test_the_same_holds_on_the_universe_path(tmp_path):
    """`_decide_universe` had its own copy of the silent return."""
    desk, rows = _drive(tmp_path, universe_mode=True)
    assert desk.stats.analyst_errors > 0
    assert [r for r in rows if r.get("kind") == "BLIND"]


def test_the_blind_row_says_what_broke(tmp_path):
    """A row that records only 'something failed' cannot tell a timeout from a
    crashed renderer, and the two need different fixes."""
    _, rows = _drive(tmp_path)
    r = next(r for r in rows if r.get("kind") == "BLIND")
    assert r["decision"]["stage"] in ("charts", "read", "survey")
    assert r["decision"]["error_type"] == "AnalystError"
    assert "never answered" in r["decision"]["error"]
    assert "BLIND" in r["reason"]


def test_a_blind_row_carries_its_forward_path(tmp_path):
    """So an outage window can be priced LATER, once we know what the market
    did through it. Dropping the row threw that away permanently."""
    _, rows = _drive(tmp_path)
    r = next(r for r in rows if r.get("kind") == "BLIND")
    assert r["path_ref"]["bar_count"] > 1
    assert r["outcome"]["returns_price"]


def test_a_blind_row_claims_no_direction(tmp_path):
    """Nothing formed a view, so the row must not assert one. Recording LONG
    would make an outage look like a long the desk passed on."""
    _, rows = _drive(tmp_path)
    r = next(r for r in rows if r.get("kind") == "BLIND")
    assert r["decision"].get("declined") is None


# ------------------------------------------------- BLIND is not a refusal

def test_blind_is_not_a_refusal_kind():
    assert not DecisionKind.BLIND.value.startswith("REFUSAL")


def test_no_downstream_refusal_filter_picks_up_a_blind_row(tmp_path):
    """The single property that makes the name safe. Both selectors below charge
    forgone value to a restriction; a blind bar has no restriction to charge."""
    import missed_money as M
    from golddesk import constitution as K
    _, rows = _drive(tmp_path)
    blind = [r for r in rows if r.get("kind") == "BLIND"]
    assert blind, "nothing blind was written; this test would pass vacuously"
    assert M.price_restrictions(blind) == []
    assert M.coverage(blind) == ["no refusals to assess"]
    # constitution's own refusal pass must see none of them either
    assert not [r for r in blind if str(r.get("kind", "")).startswith("REFUSAL")]
    assert K is not None


# --------------------------------------------------- no new silent returns

def test_every_analyst_error_handler_journals(tmp_path):
    """THE ANTI-REGRESSION GUARD, and the reason this is source-level.

    The three sites are fixed. Nothing stops a fourth from being added with the
    original `log.warning(...); return` shape — it would look completely
    ordinary in review and would silently reopen the exact hole. This walks the
    AST of live.py and requires every `except AnalystError` handler to call
    `_record_blind` (or to re-raise, which is also not silent).
    """
    tree = ast.parse(LIVE_PY.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        names = {n.id for n in ast.walk(node.type or ast.Pass()) if isinstance(n, ast.Name)}
        if "AnalystError" not in names:
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "_record_blind" in body or "Raise" in body:
            continue
        offenders.append(node.lineno)
    assert not offenders, (
        f"live.py:{offenders} swallows AnalystError without a ledger row. "
        f"A bar the analyst never answered on must be written down, or a blind "
        f"session is indistinguishable from a disciplined one.")


def test_the_ast_guard_would_actually_catch_one():
    """A guard nobody has watched fail is a guard nobody knows works."""
    src = (
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except AnalystError as e:\n"
        "        log.warning('x', e)\n"
        "        return\n")
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            names = {n.id for n in ast.walk(node.type or ast.Pass()) if isinstance(n, ast.Name)}
            if "AnalystError" in names:
                body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                if "_record_blind" not in body and "Raise" not in body:
                    found.append(node.lineno)
    assert found == [4]


# ------------------------------------------------ journalling is never fatal

def test_a_broken_journal_does_not_take_the_desk_down(tmp_path, monkeypatch):
    """An unrecorded blind bar is bad. A crashed live loop is worse."""
    bars = _bars()
    atrs, sw = atr(bars), swings(bars)
    desk = LiveDesk(BlindProvider(), Ledger(tmp_path / "l.jsonl"), NullSink(),
                    shadow=True, vision=Vision.NUMERIC_ONLY,
                    thresholds=Thresholds(fallback_min_rr=1.0),
                    measure_position_constraint=False)

    def boom(*a, **k):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(desk, "_record", boom)
    tl: list[str] = []
    for i in range(60, 200):
        if classify(bars, i, sw, atrs) is None:
            continue
        desk.on_bar(bars, i, sw, atrs, None,
                    (bars[i].close - 0.05, bars[i].close + 0.05, 1.0), tl)
    assert desk.stats.analyst_errors > 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
