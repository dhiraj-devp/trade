"""Automated tests for Button-Driven Cloud MVP workflow."""

from datetime import datetime, time, timedelta
import pytest
import pandas as pd

from nse_scanner.candles import empty_cache, merge_candles, store_previous_day
from nse_scanner.cloud_scanner import (
    evaluate_monitoring_cycle,
    ist_now,
    lock_and_save_candidates,
    prepare_candidates_scan,
    restore_today_state,
    validate_market_session,
)
from nse_scanner.store import MemoryStore
from scanner_rules import Candle


class MockGrowwClient:
    def __init__(self, auth_ok: bool = True):
        self.auth_ok = auth_ok
        self.candles_data = {}
        self.prices_data = {}

    def authenticate(self) -> tuple[bool, str]:
        if self.auth_ok:
            return True, "Mock Groww authenticated."
        return False, "Invalid API credentials."

    def five_minute_interval(self) -> str:
        return "5minute"

    def daily_interval(self) -> str:
        return "1day"

    def previous_day_window(self, trading_day: datetime):
        return trading_day - timedelta(days=5), trading_day - timedelta(seconds=1)

    def download_instruments(self):
        rows = []
        for i in range(1, 60):
            sym = f"SYM{i}"
            rows.append({
                "exchange": "NSE", "segment": "CASH", "trading_symbol": sym, "series": "EQ",
                "instrument_type": "EQ", "underlying_symbol": None, "name": f"Company {i}",
                "exchange_token": str(i), "groww_symbol": f"NSE-{sym}", "isin": f"INE{i:04d}",
            })
            rows.append({
                "exchange": "NSE", "segment": "FNO", "trading_symbol": f"{sym}25NOVFUT", "series": None,
                "instrument_type": "FUT", "underlying_symbol": sym, "expiry_date": "2026-11-26",
                "exchange_token": str(1000 + i), "groww_symbol": f"NSE-{sym}-FUT",
            })
        return pd.DataFrame(rows)

    def get_ohlc_batch(self, exchange_symbols: list[str]) -> dict:
        return {
            sym: {"close": 100.0, "open": 99.0, "high": 102.0, "low": 98.0, "volume": 50000}
            for sym in exchange_symbols
        }

    def get_ltp_batch(self, exchange_symbols: list[str]) -> dict:
        result = {}
        for sym in exchange_symbols:
            clean = sym.replace("NSE_", "")
            result[sym] = self.prices_data.get(clean, 102.0)
        return result

    def historical_candles(self, groww_symbol: str, start: datetime, end: datetime, interval: str, segment: str = "CASH"):
        symbol = groww_symbol.replace("NSE-", "")
        return {"candles": self.candles_data.get(symbol, [])}


def test_validate_market_session_weekday_and_hours():
    settings = {"market_open": "09:15", "market_close": "15:30", "developer_mode": False}

    # Weekend Saturday
    sat = datetime(2026, 11, 21, 10, 0, 0)
    valid, msg = validate_market_session(sat, settings)
    assert not valid
    assert "Weekend" in msg

    # Weekday before open (09:00)
    mon_early = datetime(2026, 11, 23, 9, 0, 0)
    valid, msg = validate_market_session(mon_early, settings)
    assert not valid
    assert "not opened yet" in msg

    # Weekday active hours (09:30)
    mon_active = datetime(2026, 11, 23, 9, 30, 0)
    valid, msg = validate_market_session(mon_active, settings)
    assert valid
    assert "active" in msg

    # Weekday after close (15:45)
    mon_late = datetime(2026, 11, 23, 15, 45, 0)
    valid, msg = validate_market_session(mon_late, settings)
    assert not valid
    assert "closed for the day" in msg


def test_validate_market_session_developer_mode_bypass():
    settings = {"market_open": "09:15", "market_close": "15:30", "developer_mode": True}
    sunday_midnight = datetime(2026, 11, 22, 2, 0, 0)
    valid, msg = validate_market_session(sunday_midnight, settings)
    assert valid
    assert "Developer mode is active" in msg


def test_prepare_candidates_scan_auth_failure():
    client = MockGrowwClient(auth_ok=False)
    store = MemoryStore()
    settings = {"market_open": "09:15", "market_close": "15:30", "developer_mode": True}
    now = datetime(2026, 11, 23, 9, 30, 0)

    res = prepare_candidates_scan(client, store, settings, now)
    assert not res["valid"]
    assert "authentication failed" in res["error"].lower()


def test_lock_and_save_candidates_blocks_neutral_and_opposite():
    store = MemoryStore()
    settings = {"developer_mode": False}
    now = datetime(2026, 11, 23, 9, 31, 0)

    # Neutral market
    prep_neutral = {
        "valid": True,
        "trading_date": "2026-11-23",
        "market": {"status": "NEUTRAL", "direction": None},
        "candidates": [],
    }
    ok, msg, _ = lock_and_save_candidates(store, prep_neutral, now, settings)
    assert not ok
    assert "NEUTRAL" in msg

    # Opposite market
    prep_opp = {
        "valid": True,
        "trading_date": "2026-11-23",
        "market": {"status": "OPPOSITE", "direction": None},
        "candidates": [{"symbol": "RELIANCE"}],
    }
    ok, msg, _ = lock_and_save_candidates(store, prep_opp, now, settings)
    assert not ok
    assert "OPPOSITE" in msg


def test_lock_and_save_candidates_prevents_duplicate_overwrite():
    store = MemoryStore()
    settings = {"developer_mode": False}
    now = datetime(2026, 11, 23, 9, 30, 0)

    prepared = {
        "valid": True,
        "trading_date": "2026-11-23",
        "market": {"status": "BULLISH", "direction": "LONG"},
        "candidates": [
            {"symbol": "RELIANCE", "name": "Reliance", "ltp": 2500.0, "change_pct": 2.5, "sources": ["Method 1"]},
            {"symbol": "TCS", "name": "TCS", "ltp": 3500.0, "change_pct": 1.8, "sources": ["Method 1"]},
        ],
        "notes": {"Method 1": "Top gainers"},
    }

    # First save succeeds
    ok, msg, doc = lock_and_save_candidates(store, prepared, now, settings)
    assert ok
    assert doc["locked"] is True
    assert doc["trading_date"] == "2026-11-23"

    # Second save for same trading day is blocked
    ok2, msg2, _ = lock_and_save_candidates(store, prepared, now + timedelta(minutes=1), settings)
    assert not ok2
    assert "already locked" in msg2.lower()


def test_restore_today_state_syncs_from_store():
    store = MemoryStore()
    trading_date = "2026-11-23"

    # Save candidates and monitoring state
    store.save_candidates({
        "trading_date": trading_date,
        "locked": True,
        "direction": "LONG",
        "candidates": [{"symbol": "INFY", "name": "Infosys", "ltp": 1600.0}],
    })
    store.save_monitoring_state(trading_date, {
        "trading_date": trading_date,
        "state": "MONITORING",
        "last_monitored_candle": "2026-11-23 09:35:00",
    })
    store.save_signal("2026-11-23_INFY_SETUP 1_LONG_09:25", {
        "trading_date": trading_date,
        "symbol": "INFY",
        "direction": "LONG",
        "setup": "SETUP 1",
    })

    restored = restore_today_state(store, trading_date)
    assert restored["locked"] is not None
    assert restored["locked"]["locked"] is True
    assert restored["monitoring_state"]["state"] == "MONITORING"
    assert len(restored["signals"]) == 1
    assert restored["signals"][0]["symbol"] == "INFY"


def test_evaluate_monitoring_cycle_setups_and_deduplication():
    store = MemoryStore()
    client = MockGrowwClient(auth_ok=True)
    settings = {
        "market_open": "09:15",
        "market_close": "15:30",
        "setup3_end": "11:00",
        "candle_minutes": 5,
        "setup2_combined_pct": 2.5,
        "setup2_second_pct": 1.0,
        "setup3_inclusion_confirmed": True,
        "developer_mode": False,
    }
    trading_date = "2026-11-23"
    now = datetime(2026, 11, 23, 9, 36, 0)

    cache = empty_cache(trading_date)
    # Store previous day
    store_previous_day(cache, "RELIANCE", [
        {"ts": "2026-11-20 00:00:00", "open": 98.0, "high": 105.0, "low": 95.0, "close": 100.0, "volume": 100000}
    ], trading_date)

    # Setup 1 test candles:
    # 09:15: Green candle (open 100, high 104, low 99, close 103)
    # 09:20: Red candle inside 09:15 range (open 102, close 101, high 102.5, low 100.5)
    # 09:25: Red candle (open 101, close 100.5, high 101.5, low 100)
    # 09:30: Red candle
    candles = [
        {"ts": "2026-11-23 09:15:00", "open": 100.0, "high": 104.0, "low": 99.0, "close": 103.0, "volume": 1000.0},
        {"ts": "2026-11-23 09:20:00", "open": 102.0, "high": 102.5, "low": 100.5, "close": 101.0, "volume": 800.0},
        {"ts": "2026-11-23 09:25:00", "open": 101.0, "high": 101.5, "low": 100.0, "close": 100.5, "volume": 700.0},
        {"ts": "2026-11-23 09:30:00", "open": 100.5, "high": 101.0, "low": 99.5, "close": 100.2, "volume": 600.0},
    ]
    merge_candles(cache, "RELIANCE", candles)
    client.candles_data["RELIANCE"] = [
        ["2026-11-23T09:15:00", 100.0, 104.0, 99.0, 103.0, 1000.0],
        ["2026-11-23T09:20:00", 102.0, 102.5, 100.5, 101.0, 800.0],
        ["2026-11-23T09:25:00", 101.0, 101.5, 100.0, 100.5, 700.0],
        ["2026-11-23T09:30:00", 100.5, 101.0, 99.5, 100.2, 600.0],
    ]

    locked_data = {
        "trading_date": trading_date,
        "locked": True,
        "direction": "LONG",
        "market": {"status": "BULLISH", "direction": "LONG", "complete": True},
        "candidates": [
            {"symbol": "RELIANCE", "name": "Reliance", "ltp": 100.2, "change_pct": 2.0, "sources": ["Method 1"]},
        ],
        "considered": [],
        "notes": {},
    }

    known_ids = set()
    result = evaluate_monitoring_cycle(client, store, locked_data, now, settings, cache=cache, known_ids=known_ids)

    assert result["board"] is not None
    assert len(result["signals"]) >= 1
    sig = result["signals"][0]
    assert sig["symbol"] == "RELIANCE"
    assert sig["direction"] == "LONG"
    assert "SETUP" in sig["setup"]

    # Verify persisted in store
    assert store.signal_exists(sig["id"])

    # Re-running monitoring cycle does NOT create duplicate signal
    result2 = evaluate_monitoring_cycle(client, store, locked_data, now + timedelta(seconds=30), settings, cache=cache, known_ids=known_ids)
    assert len(result2["new_signals"]) == 0
    assert len(result2["signals"]) == len(result["signals"])


def test_evaluate_monitoring_cycle_11am_cutoff():
    store = MemoryStore()
    client = MockGrowwClient(auth_ok=True)
    settings = {
        "market_open": "09:15",
        "market_close": "15:30",
        "setup3_end": "11:00",
        "candle_minutes": 5,
        "developer_mode": False,
    }
    trading_date = "2026-11-23"
    # Time is 11:01 AM IST
    now = datetime(2026, 11, 23, 11, 1, 0)

    locked_data = {
        "trading_date": trading_date,
        "locked": True,
        "direction": "LONG",
        "market": {"status": "BULLISH", "direction": "LONG", "complete": True},
        "candidates": [{"symbol": "TCS", "name": "TCS", "ltp": 3500.0}],
    }

    result = evaluate_monitoring_cycle(client, store, locked_data, now, settings)
    assert result["is_past_cutoff"] is True
    assert result["monitoring_state"]["state"] == "COMPLETED"


def test_gap_detection_on_reopening():
    store = MemoryStore()
    trading_date = "2026-11-23"
    client = MockGrowwClient(auth_ok=True)
    settings = {"setup3_end": "11:00", "developer_mode": False, "candle_minutes": 5}

    # Simulate app having monitored at 09:20, and then closed/slept until 10:15
    store.save_monitoring_state(trading_date, {
        "trading_date": trading_date,
        "state": "MONITORING",
        "last_monitored_candle": "2026-11-23 09:20:00",
        "last_monitored_at": "2026-11-23 09:20:30",
    })

    now_resumed = datetime(2026, 11, 23, 10, 15, 0)
    locked_data = {
        "trading_date": trading_date,
        "locked": True,
        "direction": "LONG",
        "market": {"status": "BULLISH", "direction": "LONG", "complete": True},
        "candidates": [{"symbol": "INFY", "name": "Infosys", "ltp": 1600.0}],
    }

    result = evaluate_monitoring_cycle(client, store, locked_data, now_resumed, settings)
    assert result["gap_detected"] is True
    assert "Monitoring gap detected" in result["gap_message"]


def test_prepare_candidates_scan_full_flow_and_filters(monkeypatch, tmp_path):
    client = MockGrowwClient(auth_ok=True)
    store = MemoryStore()
    settings = {
        "market_open": "09:15",
        "market_close": "15:30",
        "advance_min": 60.0,
        "decline_max": 40.0,
        "max_abs_move": 5.0,
        "top_n": 5,
        "developer_mode": True,
    }
    now = datetime(2026, 11, 24, 9, 30, 0)

    # Mock constituents
    symbols = [f"SYM{i}" for i in range(1, 51)]
    monkeypatch.setattr("nse_scanner.cloud_scanner.load_universe_and_constituents", lambda c, s, n: (
        [{"symbol": s, "name": s} for s in symbols],
        [{"symbol": s, "name": s} for s in symbols],
        {s: {"symbol": s, "name": s} for s in symbols},
        {"IT": ["SYM1", "SYM2"]},
        "Method 2 is blocked: missing Energy and India Defence indices",
    ))
    monkeypatch.setattr("nse_scanner.cloud_scanner.fill_missing_previous_closes", lambda c, m, ca, t, n: 0)

    # Distinct percentage moves to avoid tied cutoff:
    # SYM1: +6.0% (excluded by 5% filter)
    # SYM2: +4.8%
    # SYM3: +4.2%
    # SYM4: +3.6%
    # SYM5: +3.0%
    # SYM6: +2.4%
    # SYM7-SYM40: +1.0% to +2.0%
    # SYM41-SYM50: -2.0% (declines)
    prices = {}
    prices["SYM1"] = 106.0  # +6% (should be excluded)
    prices["SYM2"] = 104.8  # +4.8% (Top 1)
    prices["SYM3"] = 104.2  # +4.2% (Top 2)
    prices["SYM4"] = 103.6  # +3.6% (Top 3)
    prices["SYM5"] = 103.0  # +3.0% (Top 4)
    prices["SYM6"] = 102.4  # +2.4% (Top 5)
    for i in range(7, 41):
        prices[f"SYM{i}"] = 101.0 + (i * 0.02)
    for i in range(41, 51):
        prices[f"SYM{i}"] = 98.0

    client.prices_data = prices

    # Populate previous closes in cache
    from nse_scanner.candles import load_cache, save_cache
    cache = load_cache("2026-11-24")
    for i in range(1, 51):
        cache["previous"][f"SYM{i}"] = {"close": 100.0}
    save_cache(cache)

    progress_log = []
    def log_progress(pct, text):
        progress_log.append((pct, text))

    res = prepare_candidates_scan(client, store, settings, now, progress_callback=log_progress)

    assert res["valid"] is True
    assert res["locked"] is False
    assert len(progress_log) >= 5
    assert res["market"]["status"] == "BULLISH"
    assert res["market"]["direction"] == "LONG"

    # Extreme mover SYM1 (6% move) must be EXCLUDED by the 5% filter
    candidate_symbols = [c["symbol"] for c in res["candidates"]]
    assert "SYM1" not in candidate_symbols

    # Method 1 selected top gainers
    assert len(res["candidates"]) <= 5
    for c in res["candidates"]:
        assert abs(c["change_pct"]) <= 5.0
        assert "Method 1" in c["sources"]

    # Method 2 & 3 must be blocked
    assert "blocked" in res["notes"]["sector"].lower()
    assert "nse" in res["notes"]["nse_rank"].lower()

    # Saving and locking candidates
    ok, msg, locked_doc = lock_and_save_candidates(store, res, now, settings)
    assert ok is True, f"Failed with: {msg}"
    assert locked_doc["locked"] is True
    assert store.candidates.get("2026-11-24") is not None

