"""Measurements that cannot lie about their origin, and the fence that catches the ones that do.

THE NEGATIVE CONTROLS ARE THE POINT HERE. The first version of check_fetchers matched any
`.get()` call — including `dict.get()` — and flagged 130 functions in golddesk: render(), cost(),
matches(). A fence that noisy gets switched off, and then it is not there for the real thing. So
every positive control below is paired with a negative one.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from golddesk.feeds import (AISC_PLAUSIBLE, Measurement, Provenance, cache_read,
                            cache_write, fetch_aisc, parse_aisc)

UTC = timezone.utc
_ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("check_fetchers", _ROOT / "check_fetchers.py")
assert _spec and _spec.loader
fence = importlib.util.module_from_spec(_spec)
sys.modules["check_fetchers"] = fence
_spec.loader.exec_module(fence)


# ------------------------------------------------------------------ Measurement

class TestANumberCannotEscapeItsOrigin:
    def test_absent_cannot_carry_a_value(self):
        with pytest.raises(ValueError, match="ABSENT"):
            Measurement("x", 1395.0, Provenance.ABSENT, "wgc")

    def test_measured_cannot_be_valueless(self):
        with pytest.raises(ValueError, match="requires a value"):
            Measurement("x", None, Provenance.MEASURED, "wgc")

    def test_there_is_no_provenance_for_hardcoded(self):
        """The shipped fetcher returned a typed 1395 stamped utcnow(). There is deliberately no
        enum member that spells that, so the lie has to be written out longhand to be told."""
        assert {p.value for p in Provenance} == {"MEASURED", "STALE", "ABSENT"}

    def test_absent_is_not_usable_and_says_why(self):
        m = Measurement.absent("aisc", "wgc", "page did not parse")
        assert not m.usable and "did not parse" in m.render() and "ABSENT" in m.render()


class TestTheCacheDowngradesAndNeverUpgrades(object):
    def test_only_measured_values_are_cached(self, tmp_path):
        with pytest.raises(ValueError, match="only MEASURED"):
            cache_write(tmp_path / "a.json", Measurement.absent("aisc", "wgc", "nope"))

    def test_a_fresh_cache_reads_back_measured(self, tmp_path):
        p = tmp_path / "aisc.json"
        cache_write(p, Measurement("aisc", 1395.0, Provenance.MEASURED, "wgc",
                                   datetime.now(UTC).isoformat()))
        assert cache_read(p, "aisc", "wgc", fresh_hours=24).provenance is Provenance.MEASURED

    def test_an_old_cache_is_downgraded_to_STALE_with_its_age(self, tmp_path):
        p = tmp_path / "aisc.json"
        old = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
        cache_write(p, Measurement("aisc", 1395.0, Provenance.MEASURED, "wgc", old))
        m = cache_read(p, "aisc", "wgc", fresh_hours=24)
        assert m.provenance is Provenance.STALE and m.value == 1395.0
        assert "100h old" in m.why, "the age must travel with the value, not be reset on read"

    def test_a_missing_cache_is_ABSENT_not_a_default(self, tmp_path):
        assert cache_read(tmp_path / "nope.json", "aisc", "wgc", 24).provenance is Provenance.ABSENT

    def test_a_corrupt_cache_is_ABSENT_not_a_default(self, tmp_path):
        p = tmp_path / "bad.json"; p.write_text("{not json", encoding="utf-8")
        m = cache_read(p, "aisc", "wgc", 24)
        assert m.provenance is Provenance.ABSENT and "never as a default" in m.why


# ------------------------------------------------------------------ parsing

class TestParsingRefusesImplausibleMatches:
    @pytest.mark.parametrize("text,want", [
        ("AISC of $1,395/oz in Q2", 1395.0),
        ("All-in sustaining cost was US$1,412 per ounce", 1412.0),
        ("reported $1,480/oz", 1480.0),
    ])
    def test_it_finds_a_real_figure(self, text, want):
        assert parse_aisc(text) == want

    def test_the_live_page_phrase_that_produced_a_FALSE_MEASURED(self):
        """THE REGRESSION. This exact sentence is on gold.org, and the first live run parsed it
        as $2,012 and reported MEASURED — the series START YEAR read as a cost. It sat inside the
        plausible band, so no bound could catch it, and it was indistinguishable from a real
        figure to everything downstream. The module built to prevent fabricated measurements
        produced one on its first successful fetch."""
        page = ("This page provides the quarterly average global AISC of gold production from "
                "2012, with an AISC cost curve representing the most recent quarter available.")
        assert parse_aisc(page) is None

    def test_a_year_is_not_a_cost(self):
        assert parse_aisc("Gold Demand Trends 2026 | all-in sustaining cost data") != 2026.0
        assert parse_aisc("data series back to Q1 2010 and 2012 onwards") is None

    def test_proximity_to_the_word_AISC_is_not_evidence(self):
        """A marker is required on one side or the other. Nearness to a heading is not a unit."""
        assert parse_aisc("AISC methodology introduced 1450 mines into scope") is None

    def test_a_marked_figure_still_parses_every_common_way_it_is_written(self):
        for text in ("AISC US$1,456/oz", "$1,456 per ounce", "1,456/oz", "AISC of $1,456"):
            assert parse_aisc(text) == 1456.0, text

    def test_values_outside_the_plausible_band_are_rejected(self):
        assert parse_aisc(f"AISC of ${AISC_PLAUSIBLE[1] + 5000:,.0f}/oz") is None
        assert parse_aisc("AISC of $12/oz") is None

    def test_an_empty_or_unrelated_page_yields_None(self):
        assert parse_aisc("") is None and parse_aisc("<html>404 not found</html>") is None


class TestFetchFailsHonestlyOrNotAtAll:
    def test_a_network_failure_returns_ABSENT_with_the_reason(self):
        def boom(url): raise ConnectionError("tunnel 403")
        m = fetch_aisc(getter=boom)
        assert m.provenance is Provenance.ABSENT and "ConnectionError" in m.why

    def test_a_page_that_does_not_parse_returns_ABSENT_not_a_guess(self):
        m = fetch_aisc(getter=lambda u: "<html>nothing here</html>")
        assert m.provenance is Provenance.ABSENT and "no plausible AISC" in m.why

    def test_a_good_page_is_MEASURED_and_cached(self, tmp_path):
        p = tmp_path / "aisc.json"
        m = fetch_aisc(getter=lambda u: "AISC $1,395/oz", cache_path=p)
        assert m.provenance is Provenance.MEASURED and m.value == 1395.0
        assert json.loads(p.read_text())["provenance"] == "MEASURED"

    def test_a_failure_falls_back_to_cache_as_STALE_never_as_MEASURED(self, tmp_path):
        p = tmp_path / "aisc.json"
        old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        cache_write(p, Measurement("aisc_usd_per_oz", 1400.0, Provenance.MEASURED, "wgc", old))
        def boom(url): raise TimeoutError("slow")
        m = fetch_aisc(getter=boom, cache_path=p, fresh_hours=24)
        assert m.provenance is Provenance.STALE and m.value == 1400.0

    def test_the_absent_result_flows_into_floor_context_as_a_refusal(self):
        """End to end: the honest failure reaches the prompt as a refusal rather than a number."""
        from golddesk.supply_side import floor_context
        m = fetch_aisc(getter=lambda u: "")
        f = floor_context(spot=3300.0, atr=20.0, aisc=m.value)
        assert f.state == "UNMEASURED" and "not 'no floor'" in f.why


# ------------------------------------------------------------------ the fence

_DISCARDS = '''
import requests
def fetch_aisc(self):
    response = requests.get(self.URL, timeout=30)
    data = {"last_updated": datetime.utcnow().isoformat(), "aisc_usd_per_oz": 1395}
    return data
'''
_HONEST = '''
import requests
def fetch_aisc(url):
    r = requests.get(url, timeout=30)
    return parse(r.text)
'''
_DICT_GET = '''
def render(self):
    s = self.by_status()
    return s.get("open", []) + self.cache.get("x", [])
'''


class TestTheFenceCatchesTheShippedShape:
    def test_the_verbatim_shipped_fetcher_is_flagged(self, tmp_path):
        p = tmp_path / "f.py"; p.write_text(_DISCARDS, encoding="utf-8")
        out = fence.scan(p)
        assert len(out) == 1 and out[0]["fn"] == "fetch_aisc"

    def test_a_stamped_constant_alone_is_flagged(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text('def fetch_x():\n    return {"at": datetime.utcnow(), "v": 55}\n',
                     encoding="utf-8")
        assert "fabrication signature" in fence.scan(p)[0]["why"]


class TestTheFenceDoesNotCryWolf:
    def test_a_fetcher_that_reads_its_response_is_clean(self, tmp_path):
        p = tmp_path / "f.py"; p.write_text(_HONEST, encoding="utf-8")
        assert fence.scan(p) == []

    def test_dict_get_is_NOT_an_http_request(self, tmp_path):
        """The bug that made the first version useless: `.get()` on a dict is the same AST shape
        as `requests.get()`. Matching on the method name alone flagged 130 golddesk functions."""
        p = tmp_path / "f.py"; p.write_text(_DICT_GET, encoding="utf-8")
        assert fence.scan(p) == []

    def test_a_one_letter_local_is_not_an_http_client(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text('def report(self):\n    s = self.by_status()\n    return s.get("k")\n',
                     encoding="utf-8")
        assert fence.scan(p) == []

    def test_the_live_golddesk_package_is_clean(self):
        found = [f for pth in (_ROOT / "golddesk").rglob("*.py") for f in fence.scan(pth)]
        assert found == [], "\n".join(f"{f['file']}:{f['line']} {f['fn']}()" for f in found)


class TestTheURLChainSurvivesAVendorMovingThePage:
    def test_a_404_on_the_first_url_falls_through_to_the_next(self):
        from golddesk.feeds import WGC_AISC_URLS
        seen = []
        def getter(url):
            seen.append(url)
            if url == WGC_AISC_URLS[0]:
                raise RuntimeError("404 Not Found")
            return "AISC $1,456/oz"
        m = fetch_aisc(getter=getter)
        assert m.provenance is Provenance.MEASURED and m.value == 1456.0
        assert len(seen) == 2 and WGC_AISC_URLS[1] in m.why

    def test_every_url_failing_names_all_of_them(self):
        """The pack's dead URL was found on the first real run. When the next one dies, the
        refusal has to say which were tried, or the next person guesses again."""
        def getter(url): raise RuntimeError("404")
        m = fetch_aisc(getter=lambda u: (_ for _ in ()).throw(RuntimeError("404")))
        assert m.provenance is Provenance.ABSENT
        assert m.why.count("RuntimeError") >= 2 and "Tried" in m.why

    def test_a_page_that_fetches_but_hides_the_number_says_it_may_be_a_widget(self):
        m = fetch_aisc(getter=lambda u: "<html><div id='chart'></div></html>")
        assert "chart widget" in m.why, (
            "a JS-rendered figure is invisible to any regex over raw HTML, and the refusal "
            "should say so rather than looking like a bad pattern")

    def test_the_dead_pack_url_is_gone(self):
        from golddesk.feeds import WGC_AISC_URLS
        assert not any("all-in-sustaining-costs" in u for u in WGC_AISC_URLS), (
            "that URL 404s — inherited from the pack's fetcher, which could not notice because "
            "it discards its response")
