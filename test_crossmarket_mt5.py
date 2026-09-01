r"""Gold's macro context, read from the terminal that is already connected.

THE DEFECT. drivers_free.py fetches DXY, S&P and VIX from Yahoo. On 2026-08-27
Yahoo returned "possibly delisted; no price data found" for DX-Y.NYB, ^GSPC and
^VIX SIMULTANEOUSLY — three of the most heavily quoted series in the world do
not delist on the same afternoon, so that was the API, not the market.

Every brief that day carried MACRO CONTEXT: UNMEASURED. Gold's entire bid is
macro, and the desk was reading it blind to the dollar while holding an
authenticated connection to a broker quoting silver, the dollar, indices and oil
on the SAME CLOCK as its own bars.

This does not replace drivers_free — real yields and breakevens need a rate
curve no broker quotes. It means the analyst is not left with NOTHING when the
web feed fails.

    python3 -m pytest test_crossmarket_mt5.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.crossmarket_mt5 import SERIES, collect


class Client:
    """A terminal that carries `have` and quotes a steady drift on each."""

    def __init__(self, have=("XAGUSD", "EURUSD", "US500", "USOIL"), drift=0.001,
                 base=30.0, raises=False, short=False):
        self.have, self.drift, self.base = set(have), drift, base
        self.raises, self.short = raises, short

    def symbol_info(self, name):
        if self.raises:
            raise RuntimeError("terminal not connected")
        return object() if name in self.have else None

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if self.raises:
            raise RuntimeError("copy_rates failed")
        n = 2 if self.short else count
        return [{"close": self.base * (1 + i * self.drift)} for i in range(n)]


def _s(cm, key):
    return cm.series[key]


def test_it_reads_every_series_the_broker_carries():
    cm = collect(Client())
    assert set(cm.series) == set(SERIES)
    assert all(s.observed for s in cm.series.values())


def test_a_missing_symbol_is_ABSENT_not_dropped():
    """The analyst must be able to tell 'the dollar did nothing' from 'nobody
    looked'. Omitting the line would collapse those into the same brief."""
    cm = collect(Client(have=("XAGUSD",)))
    assert not _s(cm, "eurusd").observed
    assert "EURUSD     ABSENT" in cm.render()
    assert "Absent is not neutral" in cm.render()


def test_it_tries_every_alias_before_giving_up():
    """Retail brokers name the same instrument four different ways, and a
    missing symbol must be a MISS rather than a crash."""
    cm = collect(Client(have=("XAGUSD.r",)))
    assert _s(cm, "silver").symbol == "XAGUSD.r"


def test_a_dead_terminal_returns_a_block_rather_than_raising():
    """A context read that throws would take down the decision path. Losing
    context is a degradation; a desk that stops trading because one symbol read
    failed has converted a missing input into an outage."""
    cm = collect(Client(raises=True))
    assert not any(s.observed for s in cm.series.values())
    assert "ABSENT" in cm.render()


def test_too_few_bars_reads_ABSENT_rather_than_a_fabricated_change():
    cm = collect(Client(short=True))
    assert not _s(cm, "silver").observed


def test_the_gold_silver_ratio_is_computed_when_both_are_known():
    cm = collect(Client(base=30.0, drift=0.0), gold_price=4500.0)
    assert cm.gold_silver_ratio == pytest.approx(150.0)
    assert "GOLD/SILVER 150.0" in cm.render()


def test_no_ratio_without_a_gold_price():
    """Better absent than invented — the ratio is the one number here a reader
    would take at face value."""
    assert collect(Client()).gold_silver_ratio is None


def test_the_ratio_carries_its_interpretation():
    """A bare number teaches nothing. Widening with gold up is FEAR; narrowing
    is REFLATION — two different tapes that look identical on a gold chart."""
    text = collect(Client(), gold_price=4500.0).render()
    assert "fear" in text and "reflation" in text


def test_eurusd_is_labelled_a_proxy_and_never_the_dxy():
    """It is one pair, not a basket. Nothing may quietly treat it as the index."""
    text = collect(Client()).render()
    assert "DOLLAR PROXY, not the DXY" in text
    assert "Real yields and breakevens are not here" in text


def test_the_block_states_it_has_no_vote():
    """Same standing as every Context field: the model reasons over it, it never
    overrides structure."""
    assert "EVIDENCE ONLY" in collect(Client()).render()


def test_the_block_says_where_it_came_from():
    """'from the execution terminal' is load-bearing: a reader comparing this to
    a web quote must know why they can differ."""
    assert "from the execution terminal" in collect(Client()).render()


# ------------------------------------------------------- it is wired

def test_the_desk_actually_asks_for_it():
    """An unwired context block is the defect class this desk keeps hitting: it
    would look identical in every test while the analyst never saw a word."""
    live = (Path(__file__).parent / "golddesk" / "live.py").read_text(encoding="utf-8")
    assert "crossmarket=self._crossmarket" in live
    assert "_refresh_crossmarket(ts)" in live
    svc = (Path(__file__).parent / "golddesk" / "service.py").read_text(encoding="utf-8")
    assert "crossmarket_provider=crossmarket_fn" in svc


def test_build_brief_passes_it_into_the_blocks():
    runner = (Path(__file__).parent / "golddesk" / "runner.py").read_text(encoding="utf-8")
    assert "crossmarket: Optional[str] = None" in runner
    assert "blocks.append(crossmarket)" in runner


def test_it_is_refreshed_on_a_cadence_not_every_wake():
    """Four symbol reads in front of every M15 decision is four network round
    trips buying a number that cannot move that fast."""
    live = (Path(__file__).parent / "golddesk" / "live.py").read_text(encoding="utf-8")
    i = live.index("def _refresh_crossmarket")
    assert "self.crossmarket_refresh" in live[i:i + 900]
    assert "timedelta(minutes=5)" in live


def test_the_module_cannot_influence_a_decision():
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "crossmarket_mt5.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for f in ("Thresholds", "compile_signal", "ev_gate", "Refusal", "current_stop"):
        assert f not in names, f"crossmarket_mt5 references {f!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
