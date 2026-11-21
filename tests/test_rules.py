from datetime import datetime, time, timedelta

from scanner_rules import (
    ALTERNATE_BREADTH_DENOMINATOR,
    Candle,
    assess_quote_group,
    change_pct,
    classify_market,
    classify_price,
    eligible,
    explain_setups,
    merge_candidates,
    missing_session_slots,
    scan_setups,
    select_price_leaders,
    select_sectors,
    signal_document_id,
    snapshot_is_lockable,
)


def candle(clock, open_, high, low, close, volume):
    return Candle(datetime.strptime(f"2026-11-20 {clock}", "%Y-%m-%d %H:%M"), open_, high, low, close, volume)


def test_price_states_keep_unchanged_separate():
    assert classify_price(101, 100) == "ADVANCE"
    assert classify_price(99, 100) == "DECLINE"
    assert classify_price(100.004, 100) == "UNCHANGED"


def test_default_breadth_ignores_unchanged_in_the_denominator():
    result = classify_market(60, 40, 60, 40, nifty_unchanged=50, fno_unchanged=50)
    assert result["status"] == "BULLISH"
    assert result["direction"] == "LONG"
    assert result["nifty50"]["unchanged"] == 50
    assert result["nifty50"]["adv_pct"] == 60


def test_alternate_denominator_is_stricter_and_not_the_default():
    result = classify_market(
        60, 40, 60, 40, nifty_unchanged=50, fno_unchanged=50, denominator=ALTERNATE_BREADTH_DENOMINATOR
    )
    assert result["status"] == "NEUTRAL"
    assert result["direction"] is None


def test_breadth_boundaries_and_gates():
    assert classify_market(60, 40, 60, 40)["status"] == "BULLISH"
    assert classify_market(40, 60, 40, 60)["status"] == "BEARISH"
    assert classify_market(59, 41, 59, 41)["status"] == "NEUTRAL"
    assert classify_market(70, 30, 30, 70)["status"] == "OPPOSITE"
    assert classify_market(50, 50, 70, 30)["status"] == "NEUTRAL"
    assert classify_market(0, 0, 10, 1)["status"] == "NEUTRAL"
    assert classify_market(-1, 2, 3, 4)["status"] == "NEUTRAL"


def test_five_percent_filter_keeps_the_existing_boundary():
    assert eligible(5, "LONG", market_status="BULLISH") is True
    assert eligible(5.01, "LONG", market_status="BULLISH") is False
    assert eligible(-5, "SHORT", market_status="BEARISH") is True
    assert eligible(-5.01, "SHORT", market_status="BEARISH") is False
    assert eligible(1, "LONG", market_status="NEUTRAL") is False
    assert eligible(1, "SHORT", market_status="BULLISH") is False
    assert eligible(0, "LONG", market_status="BULLISH") is False


def test_setup_one_uses_only_the_first_three_candles():
    candles = [
        candle("09:15", 100, 102, 99, 101, 1000),
        candle("09:20", 101, 101.5, 100, 100.4, 900),
        candle("09:25", 100.5, 101, 100.2, 100.8, 800),
        candle("09:30", 100.6, 101, 100.1, 100.8, 5000),
    ]
    alerts = scan_setups(candles, "LONG", 200, 50)
    assert [item["setup"] for item in alerts] == ["SETUP 1"]
    assert alerts[0]["time"] == "09:20"


def test_setup_one_does_not_fire_without_a_previous_candle():
    candles = [
        candle("09:15", 101, 102, 99, 100, 1000),
        candle("09:20", 103, 104, 102, 103.5, 900),
    ]
    assert scan_setups(candles, "LONG", 200, 50) == []


def test_setup_two_boundaries_are_inclusive_and_absolute():
    matched = [
        candle("09:15", 100, 111, 99, 110, 1000),
        candle("09:20", 102, 103, 101, 102.5, 900),
    ]
    assert scan_setups(matched, "LONG", 110, 90)[0]["setup"] == "SETUP 2"
    too_far = [
        candle("09:15", 100, 111, 99, 110, 1000),
        candle("09:20", 102, 103, 101, 102.5001, 900),
    ]
    assert scan_setups(too_far, "LONG", 110, 90) == []
    short = [
        candle("09:15", 100, 101, 89, 99, 1000),
        candle("09:20", 99, 100, 98.5, 98.5, 800),
    ]
    alerts = scan_setups(short, "SHORT", 120, 90)
    assert [item["setup"] for item in alerts] == ["SETUP 2"]


def _through(clock, last_volume, last_open=101, last_close=100.5, volumes=None):
    start = datetime.strptime("2026-11-20 09:15", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"2026-11-20 {clock}", "%Y-%m-%d %H:%M")
    rows = []
    cursor = start
    index = 0
    while cursor <= end:
        last = cursor == end
        volume = last_volume if last else (1000 - index if volumes is None else volumes[index])
        open_ = last_open if last else 101
        close = last_close if last else 100.5
        rows.append(Candle(cursor, open_, 102, 99, close, volume))
        cursor += timedelta(minutes=5)
        index += 1
    return rows


def test_setup_three_requires_volume_below_every_earlier_candle():
    hit = [
        candle("09:15", 100, 101, 99, 100.5, 100),
        candle("09:20", 100.5, 101, 100, 100.8, 90),
        candle("09:25", 100.8, 101, 100.2, 100.9, 80),
        candle("09:30", 101, 101.2, 100.4, 100.5, 70),
    ]
    assert scan_setups(hit, "LONG", 200, 50)[0]["setup"] == "SETUP 3"
    higher_than_an_earlier = [
        candle("09:15", 100, 101, 99, 100.5, 50),
        candle("09:20", 100.5, 101, 100, 100.8, 80),
        candle("09:25", 100.8, 101, 100.2, 100.9, 70),
        candle("09:30", 101, 101.2, 100.4, 100.5, 60),
    ]
    assert scan_setups(higher_than_an_earlier, "LONG", 200, 50) == []
    equal = hit[:-1] + [candle("09:30", 101, 101.2, 100.4, 100.5, 80)]
    assert scan_setups(equal, "LONG", 200, 50) == []
    at_ten_fifty_five = _through("10:55", 1)
    assert "10:55" in [item["time"] for item in scan_setups(at_ten_fifty_five, "LONG", 200, 50) if item["setup"] == "SETUP 3"]
    at_eleven = _through("11:00", 1)
    eleven_times = [item["time"] for item in scan_setups(at_eleven, "LONG", 200, 50) if item["setup"] == "SETUP 3"]
    assert "10:55" in eleven_times and "11:00" not in eleven_times
    included = [item["time"] for item in scan_setups(at_eleven, "LONG", 200, 50, include_cutoff_candle=True) if item["setup"] == "SETUP 3"]
    assert "11:00" in included


def test_setup_one_boundaries_use_open_and_close_only():
    previous = candle("09:15", 100, 110, 90, 105, 1000)
    open_at_high = [previous, candle("09:20", 110, 120, 80, 100, 10)]
    close_at_low = [previous, candle("09:20", 100, 120, 80, 90, 10)]
    assert scan_setups(open_at_high, "LONG", 200, 50)[0]["high"] == 120
    assert scan_setups(close_at_low, "LONG", 200, 50)[0]["time"] == "09:20"
    short_open_at_low = [previous, candle("09:20", 90, 120, 80, 100, 10)]
    short_close_at_high = [previous, candle("09:20", 100, 120, 80, 110, 10)]
    assert scan_setups(short_open_at_low, "SHORT", 200, 50)[0]["setup"] == "SETUP 1"
    assert scan_setups(short_close_at_high, "SHORT", 200, 50)[0]["setup"] == "SETUP 1"
    open_outside = [previous, candle("09:20", 110.01, 120, 80, 100, 10)]
    close_outside = [previous, candle("09:20", 100, 120, 80, 89.99, 10)]
    wrong_colour = [previous, candle("09:20", 95, 120, 80, 100, 10)]
    assert scan_setups(open_outside, "LONG", 200, 50) == []
    assert scan_setups(close_outside, "LONG", 200, 50) == []
    assert scan_setups(wrong_colour, "LONG", 200, 50) == []
    assert scan_setups(open_at_high, "SHORT", 200, 50) == []


def test_setup_two_exact_caps_and_missing_previous_day_level():
    exact = [
        candle("09:15", 100, 111, 99, 110, 1000),
        candle("09:20", 100, 101, 99, 101, 900),
    ]
    assert scan_setups(exact, "LONG", 110, 90)[0]["second_move_pct"] == 1
    equal_high = [
        candle("09:15", 100, 110, 99, 105, 1000),
        candle("09:20", 100, 101, 99, 100.5, 900),
    ]
    assert scan_setups(equal_high, "LONG", 110, 90) == []
    missing = explain_setups(exact, "LONG", None, 90)
    assert all(item["setup"] != "SETUP 2" for item in missing["alerts"])
    assert any(item["result"] == "BLOCKED" and "Previous-day high" in item["label"] for item in missing["checks"])
    short_exact = [
        candle("09:15", 100, 101, 89, 99, 1000),
        candle("09:20", 99, 100, 98, 98.5, 800),
    ]
    assert scan_setups(short_exact, "SHORT", 120, 90)[0]["setup"] == "SETUP 2"
    missing_low = explain_setups(short_exact, "SHORT", 120, None)
    assert missing_low["alerts"] == []
    assert any(item["result"] == "BLOCKED" and "Previous-day low" in item["label"] for item in missing_low["checks"])


def test_setup_three_blocks_a_gap_or_a_missing_volume_and_checks_colour():
    gapped = [
        candle("09:15", 100, 101, 99, 100.5, 100),
        candle("09:20", 100, 101, 99, 100.5, 90),
        candle("09:25", 100, 101, 99, 100.5, 80),
        candle("10:55", 101, 102, 100, 100.5, 1),
    ]
    gapped_result = explain_setups(gapped, "LONG", 200, 50)
    assert gapped_result["alerts"] == []
    assert any(item["result"] == "BLOCKED" for item in gapped_result["checks"])
    missing_volume = [
        candle("09:15", 100, 101, 99, 100.5, None),
        candle("09:20", 100, 101, 99, 100.5, 90),
        candle("09:25", 100, 101, 99, 100.5, 80),
        candle("09:30", 101, 102, 100, 100.5, 1),
    ]
    blocked = explain_setups(missing_volume, "LONG", 200, 50)
    assert blocked["alerts"] == []
    assert any(item["result"] == "BLOCKED" and "volume" in item["label"] for item in blocked["checks"])
    wrong_colour = [
        candle("09:15", 100, 101, 99, 100.5, 100),
        candle("09:20", 100, 101, 99, 100.5, 90),
        candle("09:25", 100, 101, 99, 100.5, 80),
        candle("09:30", 100, 102, 99, 101, 1),
    ]
    colour = explain_setups(wrong_colour, "LONG", 200, 50)
    assert colour["alerts"] == []
    assert any(item["result"] == "FAIL" and "colour" in item["label"] for item in colour["checks"])
    correct_short = [
        candle("09:15", 100, 101, 99, 99.5, 100),
        candle("09:20", 100, 101, 99, 99.5, 90),
        candle("09:25", 100, 101, 99, 99.5, 80),
        candle("09:30", 100, 102, 99, 101, 1),
    ]
    assert scan_setups(correct_short, "SHORT", 200, 50)[0]["setup"] == "SETUP 3"


def test_doji_is_not_an_opposite_candle():
    candles = [
        candle("09:15", 100, 102, 99, 101, 1000),
        candle("09:20", 101, 102, 100, 101, 800),
    ]
    assert scan_setups(candles, "LONG", 200, 50) == []


def test_incomplete_or_stale_quotes_do_not_produce_breadth():
    now = datetime(2026, 11, 20, 9, 20)
    quotes = {
        "AAA": {"ltp": 101, "prev_close": 100, "as_of": now},
        "BBB": {"ltp": 100, "prev_close": 100, "as_of": now - timedelta(seconds=31)},
    }
    result = assess_quote_group(quotes, ["AAA", "BBB"], now, 30)
    assert result["complete"] is False
    assert result["counts"] is None
    missing = assess_quote_group({"AAA": {"ltp": 1, "prev_close": None, "as_of": now}}, ["AAA"], now, 30)
    assert missing["complete"] is False


def test_ranking_refuses_a_cutoff_tie_until_approved():
    rows = [{"symbol": symbol, "change_pct": 1.0} for symbol in ["A", "B", "C", "D", "E", "F"]]
    blocked, error = select_price_leaders(rows, "LONG", "BULLISH", limit=5, tie_break_approved=False)
    assert blocked == []
    assert "tie" in error.lower()
    chosen, error = select_price_leaders(rows, "LONG", "BULLISH", limit=5, tie_break_approved=True)
    assert error is None
    assert [row["symbol"] for row in chosen] == ["A", "B", "C", "D", "E"]


def test_price_leaders_drop_moves_beyond_five_percent():
    rows = [
        {"symbol": "BIG", "change_pct": 6},
        {"symbol": "OK", "change_pct": 4},
        {"symbol": "DOWN", "change_pct": -1},
    ]
    chosen, error = select_price_leaders(rows, "LONG", "BULLISH")
    assert error is None
    assert [row["symbol"] for row in chosen] == ["OK"]


def test_sector_ranking_needs_every_sector():
    moves = [{"sector": "Bank", "move_pct": 1.0}]
    chosen, error = select_sectors(moves, "LONG", required_names=("Bank", "IT"))
    assert chosen == []
    assert "IT" in error


def test_duplicate_symbols_keep_every_source():
    merged = merge_candidates({
        "Top movers": [{"symbol": "RELIANCE", "change_pct": 1}],
        "NSE gainers/losers": [{"symbol": "RELIANCE", "change_pct": 1}],
    })
    assert merged[0]["sources"] == ["Top movers", "NSE gainers/losers"]


def test_signal_identity_and_selection_lock():
    first = signal_document_id("2026-11-20", "RELIANCE", "SETUP 1", "2026-11-20 09:20:00", "LONG")
    assert first == signal_document_id("2026-11-20", "RELIANCE", "SETUP 1", "2026-11-20 09:20:00", "LONG")
    fresh = {"taken_at": datetime(2026, 11, 20, 9, 29, 50), "market": {"complete": True}}
    assert snapshot_is_lockable(fresh["taken_at"], datetime(2026, 11, 20, 9, 30, 10), time(9, 30), 30)
    assert not snapshot_is_lockable(datetime(2026, 11, 20, 9, 30, 1), datetime(2026, 11, 20, 9, 30, 1), time(9, 30), 30)
    assert not snapshot_is_lockable(datetime(2026, 11, 20, 9, 28), datetime(2026, 11, 20, 9, 30), time(9, 30), 30)


def test_missing_candle_slots_are_reported():
    candles = [candle("09:15", 1, 1, 1, 1, 1)]
    missing = missing_session_slots(candles, datetime(2026, 11, 20).date(), datetime(2026, 11, 20, 9, 30))
    assert missing == [datetime(2026, 11, 20, 9, 20), datetime(2026, 11, 20, 9, 25)]


def test_change_uses_rounded_prices():
    assert change_pct(100.004, 100) == 0
