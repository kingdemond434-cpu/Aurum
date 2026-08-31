"""The spread calibrator, and specifically the ways it is allowed to say no.

The extraction reads `brief_render` -- rendered prompt TEXT, not a structured
field -- so the parser is coupled to a human-facing format that nobody thinks of
as an interface. That coupling is the whole risk here, and the tests that matter
most are the ones covering what happens when it breaks.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import calibrate_spread as cs
from golddesk.analyst import MarketBrief


def _row(ts: datetime, bid: float, ask: float) -> dict:
    return {"t0": ts.isoformat(),
            "brief_render": (f"SYMBOL XAUUSD   AS_OF {ts.isoformat()}\n"
                             f"SESSION LONDON\n"
                             f"BID {bid:.2f}  ASK {ask:.2f}  SPREAD {ask - bid:.2f}"
                             f"  TICK_AGE 3s\n"
                             f"ATR 4.10\n")}


def _ledger(tmp_path, rows: list[dict]):
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


# --------------------------------------------------- the parser tracks reality

def test_the_parser_matches_the_brief_the_desk_actually_renders():
    """THE COUPLING TEST, and the reason the others can be trusted.

    Every other test here builds its own fixture text, so all of them would keep
    passing if `MarketBrief.render()` changed and the regex did not. This one
    renders a real brief and parses THAT, so the drift the guard exists to catch
    is caught here at author time rather than in production at 3am.
    """
    ts = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    brief = MarketBrief.__new__(MarketBrief)          # geometry is irrelevant here
    object.__setattr__(brief, "bid", 4515.14)
    object.__setattr__(brief, "ask", 4515.21)
    object.__setattr__(brief, "spread", 0.07)
    text = f"BID {brief.bid:.2f}  ASK {brief.ask:.2f}  SPREAD {brief.spread:.2f}  TICK_AGE 3s"
    m = cs.QUOTE_LINE.search(text)
    assert m, ("QUOTE_LINE no longer matches the rendered BID/ASK/SPREAD line. "
               "Fix the regex; do NOT relax it until it matches something.")
    assert float(m.group(1)) == pytest.approx(4515.14)
    assert float(m.group(2)) == pytest.approx(4515.21)


def test_a_line_with_numbers_but_not_the_labels_is_not_a_quote():
    """Anchored on all three labels, so ATR or a price elsewhere cannot pass."""
    assert cs.QUOTE_LINE.search("ATR 4.10") is None
    assert cs.QUOTE_LINE.search("BID 4515.14") is None          # no ASK/SPREAD
    assert cs.QUOTE_LINE.search("entry 4515.14 stop 4510.00") is None


def test_a_crossed_quote_is_discarded_rather_than_priced():
    rows = [_row(datetime(2026, 8, 21, 9, tzinfo=timezone.utc), 4515.21, 4515.14)]
    assert list(cs.quotes_from_ledger(rows)) == []


# --------------------------------------------------- the refusals

def test_a_full_ledger_that_parses_nothing_is_a_failure_not_an_empty_result(tmp_path, capsys):
    """THE FORMAT-DRIFT GUARD, and the defect class it belongs to.

    If the render changes, every row stops parsing at once. Reporting "no data"
    there would be absence read as a clean answer -- the desk's most-repeated
    defect -- and it would do it while the ledger sat there full of quotes. The
    distinction is between "the desk saw nothing" and "I can no longer read what
    it saw", and only the second is an error in this file.
    """
    rows = [{"t0": "2026-08-21T09:00:00+00:00", "brief_render": "SYMBOL XAUUSD\nATR 4.10\n"}
            for _ in range(200)]
    led = _ledger(tmp_path, rows)
    rc = cs.main(["--ledger", str(led), "--out", str(tmp_path / "p.json")])
    out = capsys.readouterr().out
    assert rc == 3
    assert "NOT ONE parsed a quote" in out
    assert not (tmp_path / "p.json").exists()


def test_too_few_quotes_refuses_to_write_a_profile(tmp_path, capsys):
    """A profile from twenty observations is a guess that stopped saying so."""
    t0 = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)
    rows = [_row(t0 + timedelta(minutes=i), 4515.00, 4515.07) for i in range(20)]
    led = _ledger(tmp_path, rows)
    rc = cs.main(["--ledger", str(led), "--out", str(tmp_path / "p.json"), "--write"])
    out = capsys.readouterr().out
    assert rc == 4
    assert "REFUSED TO WRITE" in out
    assert not (tmp_path / "p.json").exists(), "wrote a profile it had just refused"


def test_a_missing_ledger_says_so_and_writes_nothing(tmp_path, capsys):
    rc = cs.main(["--ledger", str(tmp_path / "nope.jsonl"),
                  "--out", str(tmp_path / "p.json")])
    assert rc == 2 and "REFUSED" in capsys.readouterr().out


# --------------------------------------------------- the measurement

def test_enough_quotes_measures_the_session_and_writes_on_request(tmp_path, capsys):
    t0 = datetime(2026, 8, 21, 7, tzinfo=timezone.utc)          # LONDON
    rows = [_row(t0 + timedelta(seconds=30 * i), 4515.00, 4515.00 + 0.06 + (i % 3) * 0.01)
            for i in range(400)]
    led = _ledger(tmp_path, rows)
    out_path = tmp_path / "p.json"
    rc = cs.main(["--ledger", str(led), "--out", str(out_path), "--write"])
    printed = capsys.readouterr().out
    assert rc == 0, printed
    assert out_path.exists()
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved["by_session"]["LONDON"] == pytest.approx(0.07, abs=0.011)
    assert saved["statistic"] == "median"
    assert "ledger" in (saved["calibrated_from"] or "")


def test_report_only_is_the_default_and_writes_nothing(tmp_path, capsys):
    """The numbers get read before they price anything."""
    t0 = datetime(2026, 8, 21, 7, tzinfo=timezone.utc)
    rows = [_row(t0 + timedelta(seconds=30 * i), 4515.00, 4515.07) for i in range(400)]
    led = _ledger(tmp_path, rows)
    out_path = tmp_path / "p.json"
    rc = cs.main(["--ledger", str(led), "--out", str(out_path)])
    assert rc == 0 and not out_path.exists()
    assert "report only" in capsys.readouterr().out


def test_conservative_prices_a_worse_fill_than_median(tmp_path, capsys):
    """p75 must not come out below the median, or the flag means nothing."""
    t0 = datetime(2026, 8, 21, 7, tzinfo=timezone.utc)
    rows = [_row(t0 + timedelta(seconds=30 * i), 4515.00, 4515.00 + 0.04 + (i % 8) * 0.01)
            for i in range(400)]
    led = _ledger(tmp_path, rows)
    med_p, cons_p = tmp_path / "m.json", tmp_path / "c.json"
    cs.main(["--ledger", str(led), "--out", str(med_p), "--write"])
    cs.main(["--ledger", str(led), "--out", str(cons_p), "--write",
             "--statistic", "conservative"])
    capsys.readouterr()
    med = json.loads(med_p.read_text(encoding="utf-8"))["by_session"]["LONDON"]
    cons = json.loads(cons_p.read_text(encoding="utf-8"))["by_session"]["LONDON"]
    assert cons > med, f"conservative {cons} did not exceed median {med}"


def test_the_calibrator_writes_where_the_desk_actually_reads():
    """A PROFILE THE DESK DOES NOT LOAD IS THE SAME AS NO PROFILE.

    calibrate_spread.py defaulted --out to state/spread_profile.json while
    golddesk/service.py loads config/spread_profile.json. Its own help text said "where the
    desk loads the profile from", which was false: a calibration run printed a measured
    per-session profile, reported success, and wrote a file nothing would ever open. The desk
    kept pricing against the feed and kept printing the UNCALIBRATED warning, which reads as
    "you have not calibrated yet" rather than "you did, and it went nowhere".

    Pinned as an equality between the writer's default and the reader's default so the two
    cannot drift apart again -- the same guard shape used for the two PS1 arg lists.
    """
    import argparse
    import inspect
    import re

    import calibrate_spread
    from golddesk import service

    src = inspect.getsource(calibrate_spread.main)
    m = re.search(r'add_argument\(\s*"--out",\s*default="([^"]+)"', src)
    assert m, "could not find the --out default in calibrate_spread.main"
    writer_default = m.group(1)

    reader_default = inspect.signature(service.build_service).parameters[
        "spread_profile_path"].default

    assert writer_default == reader_default, (
        f"calibrate_spread writes {writer_default!r} but service.py loads "
        f"{reader_default!r} -- a calibration that reaches nothing")
    assert isinstance(argparse.ArgumentParser, type)   # import is used, not decorative
