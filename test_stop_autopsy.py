"""A -1.0R in the ledger hides the only question that matters about a stop.

THE GAP. A stopped-out trade writes `realised_r: -1.0` and nothing else. That
number cannot separate two situations needing OPPOSITE fixes:

  THESIS WRONG    price went against the idea and kept going -> stop trading it
  STOPPED EARLY   price took the stop, then went where the idea said -> widen
                  the stop, enter later, or size smaller

Reading the first as the second keeps a broken mechanism alive. Reading the
second as the first kills a working one.

Observed 2026-08-27: a long entered at 4587.18 stopped at 4567 and price then
recovered to 4581.46 without the position. The idea was directionally right, the
trade lost a full R, and nothing in the ledger said so.

NO NEW COLLECTION. Every SIGNAL row already carries `outcome`, resolved forward
from the DECISION MOMENT and completely independent of the stop --
resolve_forward never looks at where the stop was. The TRADE_CLOSED row knows
what was kept. The two were never joined.

    python3 -m pytest test_stop_autopsy.py -q
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.stop_autopsy import PREMATURE_R, autopsy, render

T0 = "2026-08-27T13:00:00+00:00"


def _signal(t0=T0, mfe=0.2, mae=-1.4):
    return {"kind": "SIGNAL", "t0": t0,
            "outcome": {"mfe_r": mfe, "mae_r": mae}}


def _close(t0=T0, realised=-1.0, reason="STOP", mech="exhaustion-squeeze-long"):
    return {"kind": "TRADE_CLOSED", "entry_t0": t0, "realised_r": realised,
            "reason": reason, "mechanism_name": mech, "direction": "LONG"}


def test_a_stop_that_was_in_the_way_is_named_as_such():
    """The 08-27 long: right direction, stop hit first."""
    a, = autopsy([_signal(mfe=1.9), _close()])
    assert a.verdict == "STOPPED EARLY" and a.premature
    assert "the direction was right" in a.why.lower()
    assert "INCREASES capture" in a.why


def test_a_thesis_that_was_simply_wrong_is_named_as_such():
    a, = autopsy([_signal(mfe=0.05), _close()])
    assert a.verdict == "THESIS WRONG" and not a.premature
    assert "not what cost this trade" in a.why


def test_the_two_verdicts_are_separated_by_the_stated_threshold():
    """The boundary must be the documented constant, not an accident."""
    below, = autopsy([_signal(mfe=PREMATURE_R - 0.01), _close()])
    at, = autopsy([_signal(mfe=PREMATURE_R), _close()])
    assert below.verdict == "THESIS WRONG"
    assert at.verdict == "STOPPED EARLY"


def test_a_target_exit_gets_no_autopsy():
    """Nothing to explain about a trade that reached its objective."""
    assert autopsy([_signal(mfe=2.4), _close(reason="TARGET", realised=2.3)]) == []


def test_a_profitable_stop_is_still_examined():
    """A stop moved to profit still ENDED the trade — if the idea then ran to
    +3R, the trail was what cost it, and that is the same question."""
    a, = autopsy([_signal(mfe=3.0), _close(reason="PROFITABLE_STOP", realised=0.3)])
    assert a.verdict == "STOPPED EARLY"


# ------------------------------------------- absence is never a clean verdict

def test_a_close_with_no_signal_row_reads_UNMEASURED():
    """A gap in the ledger must not resolve to 'the stop was fine'."""
    a, = autopsy([_close()])
    assert a.verdict == "UNMEASURED"
    assert "UNKNOWN" in a.why
    assert not a.premature


def test_a_signal_with_no_resolved_excursion_reads_UNMEASURED():
    s = _signal()
    s["outcome"] = {"mfe_r": None, "mae_r": None}
    a, = autopsy([s, _close()])
    assert a.verdict == "UNMEASURED"


def test_unmeasured_rows_are_counted_in_neither_bucket():
    """Silently dropping them would flatter whichever conclusion is left."""
    text = render(autopsy([_close(t0="a"), _signal("b", mfe=2.0), _close(t0="b")]))
    assert "UNMEASURED    : 1" in text
    assert "stopped early : 1" in text
    assert "thesis wrong  : 0" in text


# ------------------------------------------------ it refuses to overclaim

def test_an_empty_sample_is_not_a_clean_bill_of_health():
    assert "not a clean bill of health" in render([])


def test_a_thin_sample_says_it_decides_nothing():
    """Four premature stops must not read as a mandate to widen every stop."""
    rows = []
    for k in range(4):
        rows += [_signal(t0=str(k), mfe=2.0), _close(t0=str(k))]
    text = render(autopsy(rows))
    assert "DECIDES NOTHING" in text
    assert "n=4" in text


def test_a_real_sample_recommends_testing_per_mechanism_not_pooled():
    """Different mechanisms need different room; the pooled number hides it."""
    rows = []
    for k in range(25):
        rows += [_signal(t0=str(k), mfe=2.0), _close(t0=str(k))]
    text = render(autopsy(rows))
    assert "DECIDES NOTHING" not in text
    assert "PER MECHANISM" in text


# ----------------------------------------------------- it is not a gate

def test_the_module_cannot_change_a_stop_or_refuse_a_trade():
    """Source-level: the danger is a later edit turning a REPORT into an
    automatic stop-widener. Walks the AST rather than grepping, because the
    docstring names the things it must not do."""
    tree = ast.parse((Path(__file__).parent / "golddesk" / "stop_autopsy.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update(a.name for a in n.names)
            names.add(getattr(n, "module", "") or "")
    for forbidden in ("Refusal", "compile_signal", "Thresholds", "current_stop",
                      "is_enforcing", "ev_gate"):
        assert forbidden not in names, (
            f"stop_autopsy references {forbidden!r} — it is a report, not a control")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
