"""Three silent corruptions live in this layer: deals mistaken for trades,
broker time mistaken for UTC, and a re-run double-counting. Most of these tests
are about those.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from golddesk.ingest import (
    Deal, IngestError, IngestLog, deals_to_trades, ingest_file, parse_api_deals,
    parse_csv, parse_mt5_html, parse_time)

UTC = timezone.utc


def d(ticket, entry="IN", *, pos="", sym="XAUUSD", act="BUY", vol=0.10,
      px=2000.0, mins=0, profit=None):
    return Deal(ticket=str(ticket), position_id=pos, symbol=sym, action=act,
                entry=entry, volume=vol, price=px,
                time_utc=datetime(2026, 6, 1, 8, 0, tzinfo=UTC) + timedelta(minutes=mins),
                profit=profit)


# --------------------------------------------------------- deals are not trades

def test_an_entry_and_an_exit_become_ONE_trade():
    """The naive parser treats each deal as a trade: double the count, half the
    size, and every position an instant round trip at the wrong price."""
    trades, un = deals_to_trades([d(1, "IN", pos="P1"),
                                  d(2, "OUT", pos="P1", px=2010.0, mins=60)])
    assert len(trades) == 1 and not un
    assert trades[0].open_price == 2000.0 and trades[0].close_price == 2010.0


def test_a_position_closed_in_three_partials_is_still_one_trade():
    """Counting it as three inflates the trade count, flatters the win rate
    whenever the winning tranche closes separately, and turns one entry into
    what looks like a basket."""
    trades, un = deals_to_trades([
        d(1, "IN", pos="P1", vol=0.30),
        d(2, "OUT", pos="P1", vol=0.10, px=2010.0, mins=10),
        d(3, "OUT", pos="P1", vol=0.10, px=2020.0, mins=20),
        d(4, "OUT", pos="P1", vol=0.10, px=2030.0, mins=30)])
    assert len(trades) == 1
    assert trades[0].close_price == pytest.approx(2020.0)   # volume-weighted
    assert trades[0].close_utc.minute == 30


def test_the_exit_price_of_a_partial_close_is_volume_weighted():
    """A single close price for a position closed in tranches is a fiction."""
    trades, _ = deals_to_trades([
        d(1, "IN", pos="P1", vol=0.30),
        d(2, "OUT", pos="P1", vol=0.20, px=2010.0, mins=10),
        d(3, "OUT", pos="P1", vol=0.10, px=2040.0, mins=20)])
    assert trades[0].close_price == pytest.approx((0.2 * 2010 + 0.1 * 2040) / 0.3)


def test_an_open_position_is_returned_unmatched_not_dropped():
    """An entry with no exit is a fact about the data."""
    trades, un = deals_to_trades([d(1, "IN", pos="P1")])
    assert trades == [] and len(un) == 1


def test_an_export_starting_mid_position_keeps_its_orphan_exit():
    trades, un = deals_to_trades([d(9, "OUT", pos="P9", mins=5)])
    assert trades == [] and len(un) == 1


def test_positions_are_paired_by_id_when_the_source_supplies_one():
    trades, _ = deals_to_trades([
        d(1, "IN", pos="A"), d(2, "IN", pos="B", px=2100.0, mins=1),
        d(3, "OUT", pos="B", px=2110.0, mins=10),
        d(4, "OUT", pos="A", px=2005.0, mins=20)])
    by_open = {t.open_price: t.close_price for t in trades}
    assert by_open[2100.0] == 2110.0 and by_open[2000.0] == 2005.0


def test_a_statement_without_position_ids_pairs_FIFO_per_symbol_and_side():
    """The MT5 statement case: a statement never prints the position id. Note
    the exits are SELL deals, because that is what closing a long looks like."""
    trades, un = deals_to_trades([
        d(1, "IN", act="BUY"), d(2, "IN", act="BUY", px=1990.0, mins=5),
        d(3, "OUT", act="SELL", px=2005.0, mins=10),
        d(4, "OUT", act="SELL", px=1995.0, mins=15)])
    assert len(trades) == 2 and not un


def test_a_sell_exit_closes_a_buy_position():
    """MT5 semantics, and I had this backwards. A long is closed by a SELL deal,
    so grouping on the raw deal type files the entry under BUY and its own exit
    under SELL and they never pair."""
    trades, un = deals_to_trades([d(1, "IN", act="BUY"),
                                  d(2, "OUT", act="SELL", px=2010.0, mins=10)])
    assert len(trades) == 1 and not un
    assert trades[0].direction == "BUY" and trades[0].pnl_price() == pytest.approx(10.0)


def test_a_buy_exit_closes_a_short_and_the_direction_is_recorded_as_SELL():
    trades, un = deals_to_trades([d(1, "IN", act="SELL"),
                                  d(2, "OUT", act="BUY", px=1990.0, mins=10)])
    assert len(trades) == 1 and trades[0].direction == "SELL"
    assert trades[0].pnl_price() == pytest.approx(10.0)


def test_two_opposing_entries_are_two_positions_not_one_closed_trade():
    """A hedge opens a second position; it does not close the first."""
    trades, un = deals_to_trades([d(1, "IN", act="BUY"),
                                  d(2, "IN", act="SELL", mins=10)])
    assert trades == [] and len(un) == 2


# ------------------------------------------------------- broker time is not UTC

def test_the_server_offset_is_required_and_has_no_default(tmp_path):
    """THE ERROR THAT SHOWS NO SYMPTOM. Parsing broker time as UTC shifts every
    trade two or three hours and misaligns every session inference, while every
    timestamp still looks perfectly ordinary."""
    p = tmp_path / "x.csv"
    p.write_text("ticket,symbol,type,entry,volume,price,time\n"
                 "1,XAUUSD,BUY,in,0.1,2000,2026.06.01 10:00:00\n", encoding="utf-8")
    with pytest.raises(IngestError, match="server_offset_hours is required"):
        ingest_file(p)


def test_the_offset_actually_shifts_the_timestamp():
    a = parse_time("2026.06.01 10:00:00", 0.0)
    b = parse_time("2026.06.01 10:00:00", 3.0)
    assert (a - b) == timedelta(hours=3)
    assert b.hour == 7 and b.tzinfo is timezone.utc


def test_an_unparseable_time_is_refused_rather_than_defaulted():
    """A wrong timestamp is worse than a missing one: it aligns the trade
    against the wrong bars and nothing downstream can tell."""
    with pytest.raises(IngestError, match="unparseable timestamp"):
        parse_time("last tuesday", 0.0)


def test_common_statement_layouts_all_parse():
    for s in ("2026.06.01 10:00:00", "2026-06-01 10:00:00", "2026.06.01 10:00",
              "01.06.2026 10:00:00"):
        assert parse_time(s, 0.0).year == 2026


def test_the_api_path_does_not_demand_an_offset():
    """Epoch seconds are already UTC — the one source where the timezone is not
    the caller's problem."""
    rows = [{"ticket": 1, "position_id": "P", "symbol": "XAUUSD", "type": 0,
             "entry": 0, "volume": 0.1, "price": 2000.0,
             "time": datetime(2026, 6, 1, 8, tzinfo=UTC).timestamp()}]
    assert parse_api_deals(rows)[0].time_utc.hour == 8


# --------------------------------------------------------------- re-ingestion

def test_the_same_deal_twice_is_counted_once():
    log = IngestLog()
    assert len(log.add([d(1), d(2, "OUT", mins=10)])) == 2
    assert log.add([d(1), d(2, "OUT", mins=10)]) == []


def test_an_overlapping_export_adds_only_what_is_new():
    log = IngestLog()
    log.add([d(1), d(2, "OUT", mins=10)])
    fresh = log.add([d(2, "OUT", mins=10), d(3, "IN", mins=20)])
    assert [f.ticket for f in fresh] == ["3"]


def test_dedup_is_on_the_brokers_ticket_not_a_content_hash():
    """A corrected row must not become a second trade."""
    log = IngestLog()
    log.add([d(1, px=2000.0)])
    assert log.add([d(1, px=2001.0)]) == []


def test_a_deal_with_no_ticket_is_not_silently_accepted():
    log = IngestLog()
    assert log.add([d("")]) == []


def test_the_log_round_trips(tmp_path):
    log = IngestLog()
    log.add([d(1, pos="P1"), d(2, "OUT", pos="P1", px=2010.0, mins=30)])
    p = tmp_path / "deals.json"
    log.save(p)
    back = IngestLog.load(p)
    assert len(back.deals) == 2
    assert len(back.trades()[0]) == 1


def test_loading_a_missing_log_is_empty_not_an_error(tmp_path):
    assert IngestLog.load(tmp_path / "nope.json").deals == []


# ------------------------------------------------------------------ parsers

HTML = """<html><body><table>
<tr><th>Time</th><th>Deal</th><th>Symbol</th><th>Type</th><th>Direction</th>
<th>Volume</th><th>Price</th><th>Order</th><th>Commission</th><th>Fee</th>
<th>Swap</th><th>Profit</th><th>Balance</th><th>Comment</th></tr>
<tr><td>2026.06.01 10:00:00</td><td>101</td><td>XAUUSD</td><td>buy</td>
<td>in</td><td>0.10</td><td>2 000.00</td><td>55</td><td>0</td><td>0</td>
<td>0</td><td>0.00</td><td>1 000.00</td><td>mrcap</td></tr>
<tr><td>2026.06.01 12:30:00</td><td>102</td><td>XAUUSD</td><td>sell</td>
<td>out</td><td>0.10</td><td>2 010.00</td><td>55</td><td>0</td><td>0</td>
<td>0</td><td>100.00</td><td>1 100.00</td><td>tp</td></tr>
<tr><td>2026.06.01 12:31:00</td><td>103</td><td></td><td>balance</td>
<td></td><td></td><td></td><td></td><td></td><td></td><td></td>
<td>0</td><td>1 100.00</td><td>deposit</td></tr>
</table></body></html>"""


def test_an_mt5_statement_parses_into_deals():
    deals = parse_mt5_html(HTML, 3.0)
    assert len(deals) == 2
    assert deals[0].symbol == "XAUUSD" and deals[0].volume == 0.10


def test_thousands_separators_and_nbsp_do_not_break_prices():
    assert parse_mt5_html(HTML, 0.0)[0].price == 2000.0


def test_balance_and_credit_rows_are_skipped():
    """They are not trades and counting them would corrupt every average."""
    assert all(d.symbol == "XAUUSD" for d in parse_mt5_html(HTML, 0.0))


def test_the_statement_offset_is_applied():
    assert parse_mt5_html(HTML, 3.0)[0].time_utc.hour == 7


def test_a_statement_round_trips_into_one_paired_trade():
    trades, un = deals_to_trades(parse_mt5_html(HTML, 3.0))
    assert len(trades) == 1 and not un
    assert trades[0].pnl_price() == pytest.approx(10.0)


CSV = ("Ticket,Position,Symbol,Type,Entry,Volume,Price,Time,S/L,T/P,Profit\n"
       "201,P1,XAUUSD,BUY,in,0.10,2000.00,2026.06.01 10:00:00,1990.00,2030.00,0\n"
       "202,P1,XAUUSD,SELL,out,0.10,2030.00,2026.06.01 14:00:00,,,300\n")


def test_a_csv_export_parses_with_aliased_headers():
    deals = parse_csv(CSV, 0.0)
    assert len(deals) == 2 and deals[0].sl == 1990.0


def test_stops_survive_the_round_trip_because_normalisation_needs_them():
    """Fixed-RISK normalisation is only possible where the stop is known."""
    trades, _ = deals_to_trades(parse_csv(CSV, 0.0))
    assert trades[0].sl == 1990.0


def test_ingesting_a_file_reports_what_it_did(tmp_path):
    p = tmp_path / "h.html"
    p.write_text(HTML, encoding="utf-8")
    log, n, note = ingest_file(p, server_offset_hours=3.0)
    assert n == 2 and "2 deal(s) parsed" in note and "1 paired trade" in note


def test_ingesting_the_same_file_twice_adds_nothing(tmp_path):
    p = tmp_path / "h.html"
    p.write_text(HTML, encoding="utf-8")
    log, n1, _ = ingest_file(p, server_offset_hours=3.0)
    log, n2, note = ingest_file(p, server_offset_hours=3.0, log=log)
    assert n1 == 2 and n2 == 0 and "2 already seen" in note


def test_the_ingested_trades_feed_the_reverse_engineering_module(tmp_path):
    """The two modules are one pipeline, not two libraries."""
    from golddesk.reverse import build_baskets, infer_structure
    p = tmp_path / "h.html"
    p.write_text(HTML, encoding="utf-8")
    log, _, _ = ingest_file(p, server_offset_hours=3.0)
    trades, _ = log.trades()
    assert infer_structure(build_baskets(trades)).kind == "SINGLE_ENTRY"
