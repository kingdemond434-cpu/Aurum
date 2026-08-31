"""ETF tonnage and speculative positioning -- the two of five flow sources that are real.

The network is never touched here: the fetcher is injected, so parsing, staleness and the
cache-fallback path are all testable on a laptop with no internet. That is this desk's own
rule -- a test that needs the network is a test that will not run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from golddesk.flows import (COT_GOLD_CODE, FlowState, collect, load, parse_cot,
                            parse_sge, sge_premium,
                            parse_gld, save)

GLD_CSV = (
    'Date,"Total Net Asset Value in the Trust ($)","Tonnes in the Trust"\n'
    '27-Aug-2026,"98,000,000,000","880.5"\n'
    '26-Aug-2026,"97,500,000,000","884.2"\n'
    '25-Aug-2026,"97,000,000,000","886.0"\n'
    + "".join(f'{d}-Aug-2026,"96,000,000,000","890.{d % 10}"\n' for d in range(24, 3, -1))
)

# The CFTC Socrata shape: NAMED fields, so a layout change cannot silently shift a column.
COT_ROW = json.dumps([{
    "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
    "cftc_contract_market_code": COT_GOLD_CODE,
    "report_date_as_yyyy_mm_dd": "2026-08-27T00:00:00.000",
    "m_money_positions_long_all": "180000",
    "m_money_positions_short_all": "45000",
}])


def test_gld_parses_latest_tonnage_and_the_changes():
    tonnes, as_of, series = parse_gld(GLD_CSV)
    assert tonnes == 880.5
    assert as_of == "2026-08-27"
    assert series[0] == 880.5 and len(series) > 20


def test_an_unrecognised_gld_header_refuses_rather_than_guessing_a_column():
    """A silently mis-parsed column is a plausible WRONG number, the worst kind. SPDR has
    moved its column order before, so this must fail loudly rather than index blindly."""
    tonnes, as_of, series = parse_gld('a,b,c\n1,2,3\n')
    assert tonnes is None and as_of is None and series == []


def test_cot_nets_managed_money_long_against_short():
    net, as_of = parse_cot(COT_ROW)
    assert net == 180000 - 45000
    assert as_of == "2026-08-27"


def test_an_absent_gold_row_is_none_not_zero():
    """There is no such thing as a zero net position by absence."""
    net, as_of = parse_cot(json.dumps([{
        "cftc_contract_market_code": "084691",
        "report_date_as_yyyy_mm_dd": "2026-08-27T00:00:00.000",
        "m_money_positions_long_all": "10", "m_money_positions_short_all": "20"}]))
    assert net is None and as_of is None


def test_a_failed_fetch_falls_back_to_cache_per_series_not_all_or_nothing(tmp_path: Path):
    """GLD and COT publish on different calendars and fail independently. One source being
    down must not blank the other."""
    cache = tmp_path / "flows.json"
    save(FlowState(gld_tonnes=900.0, gld_as_of="2026-08-20", mm_net=111_000,
                   cot_as_of="2026-08-18"), cache)

    def only_cot_works(url: str) -> str:
        if "cftc" in url:
            return COT_ROW
        raise TimeoutError("spdr unreachable")

    st = collect(cache, getter=only_cot_works)
    assert st.mm_net == 135_000, "the working source must update"
    assert st.gld_tonnes == 900.0, "the failed source must fall back, not blank"
    assert "gld" in st.errors and "cot" not in st.errors


def test_an_empty_cache_and_a_dead_network_render_unmeasured_not_zero(tmp_path: Path):
    def dead(url: str) -> str:
        raise ConnectionError("no route")

    st = collect(tmp_path / "none.json", getter=dead)
    assert st.gld_tonnes is None and st.mm_net is None
    p = st.to_prompt()
    assert "UNMEASURED" in p
    assert "0.0t" not in p, "absence rendered as a measured zero"


def test_stale_values_are_refused_by_age_rather_than_quietly_reasoned_from():
    """A cached value is normal -- both series update on their own calendar -- so AGE is what
    decides whether it can be reasoned from. GLD past 5d and COT past 14d must say so."""
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).date().isoformat()
    st = FlowState(gld_tonnes=880.0, gld_as_of=old, mm_net=120_000, cot_as_of=old)
    p = st.to_prompt(now=now)
    assert p.count("STALE") == 2
    assert "880" not in p, "a stale number must not be rendered as usable"


def test_fresh_values_render_the_flow_and_the_crowding():
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    st = FlowState(gld_tonnes=880.5, gld_change_1d=-3.7, gld_change_5d=-9.2,
                   gld_change_20d=+4.0, gld_as_of="2026-08-27",
                   mm_net=135_000, mm_net_change=-8_000, mm_pctile_52w=0.82,
                   cot_as_of="2026-08-25")
    p = st.to_prompt(now=now)
    assert "880.5t" in p and "-3.7t" in p
    assert "+135,000" in p and "82%" in p
    assert "no verdict" in p, "flows are evidence, never a direction"


def test_the_cache_round_trips(tmp_path: Path):
    cache = tmp_path / "f.json"
    save(FlowState(gld_tonnes=1.5, mm_net=-2), cache)
    assert load(cache).gld_tonnes == 1.5 and load(cache).mm_net == -2
    assert load(tmp_path / "missing.json").gld_tonnes is None


SGE_JSON = json.dumps({"data": [
    {"instid": "Ag(T+D)", "close": "8.20", "date": "2026-08-27"},
    {"instid": "Au99.99", "close": "742.15", "date": "2026-08-27"},
]})


def test_sge_finds_the_gold_contract_by_name():
    price, as_of = parse_sge(SGE_JSON)
    assert price == 742.15 and as_of == "2026-08-27"


def test_an_unrecognised_sge_payload_refuses_rather_than_guessing():
    """A mis-read gold price would be a plausible number in the prompt every day, and the
    premium computed from it confidently wrong rather than absent."""
    assert parse_sge('{"totally":"different"}') == (None, None)
    assert parse_sge("not json") == (None, None)


def test_the_premium_conversion_is_arithmetic_anyone_can_check():
    """CNY/gram -> USD/oz. Both legs are stored on the state precisely so this can be audited
    rather than believed: a wrong USDCNY moves the premium by more than the premium itself is."""
    usd_oz, prem = sge_premium(742.15, 7.12, 3230.0)
    assert usd_oz == round(742.15 / 7.12 * 31.1034768, 2)
    assert prem == round(usd_oz - 3230.0, 2)


def test_shanghai_over_and_under_london_both_read_correctly():
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    over = FlowState(sge_cny_gram=742.15, usdcny=7.12, london_usd_oz=3230.0,
                     sge_usd_oz=3242.06, sge_premium_usd_oz=12.06, sge_as_of="2026-08-27")
    under = FlowState(sge_cny_gram=730.0, usdcny=7.12, london_usd_oz=3230.0,
                      sge_usd_oz=3189.0, sge_premium_usd_oz=-41.0, sge_as_of="2026-08-27")
    assert "+12.06 USD/oz" in over.to_prompt(now=now) and "OVER London" in over.to_prompt(now=now)
    assert "-41.00 USD/oz" in under.to_prompt(now=now) and "UNDER London" in under.to_prompt(now=now)


def test_a_stale_shanghai_premium_is_refused_by_age():
    """SGE publishes on Chinese trading days, so a holiday week is normal -- but past the limit
    the premium is an assumption about a market that has since moved."""
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    st = FlowState(sge_premium_usd_oz=12.06, sge_usd_oz=3242.06, london_usd_oz=3230.0,
                   sge_cny_gram=742.15, usdcny=7.12,
                   sge_as_of=(now - timedelta(days=30)).date().isoformat())
    p = st.to_prompt(now=now)
    assert "STALE" in p and "12.06" not in p


def test_the_premium_is_all_or_nothing_across_its_three_legs(tmp_path: Path):
    """A premium computed from a stale FX rate is not a smaller measurement, it is a different
    and wrong one -- so a missing leg must fall back to cache, never part-compute."""
    cache = tmp_path / "f.json"
    save(FlowState(sge_premium_usd_oz=5.0, sge_as_of="2026-08-20"), cache)

    def sge_ok_fx_dead(url: str) -> str:
        if "sge.com.cn" in url:
            return SGE_JSON
        raise TimeoutError("yahoo down")

    st = collect(cache, getter=sge_ok_fx_dead)
    assert st.sge_premium_usd_oz == 5.0, "must fall back whole, not compute a partial premium"
    assert "sge" in st.errors
