r"""The analyst read gold with no macro at all, for days.

WHAT HAPPENED. Every brief on the live desk carried `MACRO CONTEXT: UNMEASURED`
— no dollar, no risk state, no rate context — for an instrument whose entire bid
is macro. The desk's self-audit said so plainly and nothing acted on it:

    [BROKEN] macro   all 20 recent briefs carried MACRO UNMEASURED — the analyst
                     is reading gold with no DXY, no real yield and no risk proxy

The cause was one unofficial web endpoint. `drivers_free` goes through yfinance;
on 2026-08-27 Yahoo returned "possibly delisted" for DX-Y.NYB, ^GSPC and ^VIX
simultaneously, and on 2026-08-28 the box could not import yfinance at all. One
broken dependency removed an entire category of input from the analyst while
every component reported healthy — and the self-heal's remedy (refetch) had
already been tried three times and given up.

Meanwhile the desk holds an authenticated MT5 connection quoting the dollar and
equities on the same clock as its own bars.

    python3 -m pytest test_drivers_mt5.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.crossmarket_mt5 import CrossMarket, Series
from golddesk.drivers_free import DriverPoint
from golddesk.drivers_mt5 import build_from, from_crossmarket

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _cm(eur=-0.42, eq=0.85):
    return CrossMarket({
        "eurusd": Series("eurusd", "EURUSD", eur, 1.0821),
        "equities": Series("equities", "US500", eq, 5430.2),
        "silver": Series("silver", "XAGUSD", 1.2, 29.4),
        "oil": Series("oil", None, None, None),
    })


def _web(**kw):
    """A web reading. Anything not named came back unobserved."""
    out = {}
    for k in ("dxy", "spx", "vix", "real_yield_10y", "breakeven_10y"):
        v = kw.get(k)
        out[k] = DriverPoint(k, v, None, NOW, "yahoo", True, "")
    return out


# --------------------------------------------------------------------------
# The sign. This is the one that must not be wrong.

def test_a_weaker_euro_reads_as_a_STRONGER_dollar():
    """attribution.py raises a SIGN VIOLATION when a fitted beta contradicts the
    declared one, so a proxy fed in with the wrong sense would fire that alarm
    forever while the market did nothing unusual. EURUSD down = dollar up."""
    d = from_crossmarket(_cm(eur=-0.42))["dxy"]
    assert d.change_pct == pytest.approx(0.42)


def test_a_stronger_euro_reads_as_a_weaker_dollar():
    d = from_crossmarket(_cm(eur=+0.61))["dxy"]
    assert d.change_pct == pytest.approx(-0.61)


# --------------------------------------------------------------------------
# Exact versus proxy, which decides how much weight the read carries.

def test_the_dollar_is_labelled_a_PROXY_and_says_how_close_a_one():
    d = from_crossmarket(_cm())["dxy"]
    assert d.exact is False
    assert "57%" in d.why or "58%" in d.why
    assert "EURUSD" in d.source and "inverted" in d.source


def test_the_index_is_EXACT_because_a_CFD_on_it_is_it():
    d = from_crossmarket(_cm())["spx"]
    assert d.exact is True
    assert d.change_pct == pytest.approx(0.85)


# --------------------------------------------------------------------------
# What it refuses to invent. The larger half.

def test_it_does_NOT_manufacture_a_vix():
    """Every cheap stand-in (range, ATR) is derived from price the desk already
    sends. That is not a second observation, it is the first one rearranged, and
    feeding both to a regression manufactures collinearity that looks like two
    independent drivers."""
    assert "vix" not in from_crossmarket(_cm())


def test_it_does_NOT_manufacture_rates():
    out = from_crossmarket(_cm())
    assert "real_yield_10y" not in out and "breakeven_10y" not in out


def test_a_broker_quoting_nothing_yields_nothing_rather_than_zeros():
    empty = CrossMarket({"eurusd": Series("eurusd", None, None, None),
                         "equities": Series("equities", None, None, None)})
    assert from_crossmarket(empty) == {}


# --------------------------------------------------------------------------
# It fills gaps only. A real series is never overridden by a proxy.

def test_a_working_web_dollar_is_not_replaced_by_the_proxy():
    points, note = build_from(_web(dxy=0.31), _cm(eur=-0.42))
    assert points["dxy"].change_pct == pytest.approx(0.31)
    assert points["dxy"].exact is True
    assert "dxy" not in note


def test_a_dead_web_feed_is_rescued():
    """The actual live case: yfinance not importable, so every driver came back
    unobserved and the analyst got UNMEASURED."""
    points, note = build_from(_web(), _cm())
    assert points["dxy"].observed and points["spx"].observed
    assert "EXECUTION TERMINAL" in note


def test_an_absent_web_dict_entirely_is_handled():
    points, _ = build_from(None, _cm())
    assert points["dxy"].observed


def test_the_note_names_what_is_STILL_missing():
    """'The analyst had macro today' must never be confused with 'the analyst
    had the macro it should have had'."""
    _, note = build_from(_web(), _cm())
    for absent in ("vix", "real_yield_10y", "breakeven_10y"):
        assert absent in note
    assert "still ABSENT" in note


def test_a_fully_working_web_feed_produces_no_note_and_no_change():
    before = _web(dxy=0.1, spx=0.2, vix=-1.0, real_yield_10y=0.01,
                  breakeven_10y=0.02)
    points, note = build_from(before, _cm())
    assert note == ""
    assert all(points[k] is before[k] for k in before)


# --------------------------------------------------------------------------
# It can never be the reason a wake fails.

def test_a_broken_terminal_reading_does_not_take_the_desk_down():
    """This is a RESCUE path. A rescue that can take down the thing it rescues
    is worse than no rescue."""
    class Exploding:
        @property
        def series(self):
            raise RuntimeError("mt5 pipe closed")

    points, note = build_from(_web(dxy=0.3), Exploding())
    assert points["dxy"].change_pct == pytest.approx(0.3)
    assert note == ""


def test_it_reaches_the_analyst_as_a_rendered_macro_block():
    """END TO END. The whole complaint was that briefs said UNMEASURED, so it is
    not enough that DriverPoints exist — macro_context has to render them."""
    from golddesk.macro_context import from_drivers
    points, _ = build_from(_web(), _cm())
    text = from_drivers(points, now=NOW).render()
    assert "UNMEASURED" not in text.split("\n")[0], text
    assert "dxy" in text.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
