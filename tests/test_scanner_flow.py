from datetime import datetime

import pandas as pd

from nse_scanner.candles import empty_cache, merge_candles, parse_groww_candles, store_previous_day
from nse_scanner.engine import choose_candidates, quotes_from_prices, signals_for_candidate
from nse_scanner.historical import run_historical
from nse_scanner.readiness import readiness
from nse_scanner.secrets import secret_status, update_secrets
from nse_scanner.settings import load_settings, save_settings
from nse_scanner.universe import cash_equities, derive_fno_equity_universe, nearest_futures, stable_hash
from scanner_rules import Candle


def master_frame():
    return pd.DataFrame([
        {"exchange": "NSE", "segment": "CASH", "trading_symbol": "RELIANCE", "series": "EQ", "instrument_type": "EQ",
         "underlying_symbol": None, "name": "Reliance Industries", "exchange_token": "2885", "groww_symbol": "NSE-RELIANCE", "isin": "INE002A01018"},
        {"exchange": "NSE", "segment": "FNO", "trading_symbol": "RELIANCE25NOVFUT", "series": None, "instrument_type": "FUT",
         "underlying_symbol": "RELIANCE", "expiry_date": "2026-11-26", "exchange_token": "10", "groww_symbol": "NSE-RELIANCE-FUT"},
        {"exchange": "NSE", "segment": "FNO", "trading_symbol": "NIFTY25NOVFUT", "series": None, "instrument_type": "FUT",
         "underlying_symbol": "NIFTY", "expiry_date": "2026-11-26", "exchange_token": "11", "groww_symbol": "NSE-NIFTY-FUT"},
        {"exchange": "NSE", "segment": "CASH", "trading_symbol": "NIFTY", "series": "INDEX", "instrument_type": "INDEX",
         "underlying_symbol": None, "name": "Nifty 50", "exchange_token": "NIFTY", "groww_symbol": "NSE-NIFTY"},
        {"exchange": "NSE", "segment": "CASH", "trading_symbol": "TCS", "series": "EQ", "instrument_type": "EQ",
         "underlying_symbol": None, "name": "TCS", "exchange_token": "11536", "groww_symbol": "NSE-TCS", "isin": "INE467B01029"},
    ])


def test_fno_universe_keeps_equities_and_drops_index_underlyings():
    stocks = derive_fno_equity_universe(master_frame())
    assert [item["symbol"] for item in stocks] == ["RELIANCE"]
    assert "NIFTY" not in cash_equities(master_frame())
    assert "RELIANCE" in cash_equities(master_frame())
    futures = nearest_futures(master_frame(), ["RELIANCE", "NIFTY"], "2026-11-20")
    assert futures["RELIANCE"]["trading_symbol"] == "RELIANCE25NOVFUT"
    assert stable_hash(["RELIANCE"]) == stable_hash(["RELIANCE"])


def test_secrets_round_trip_never_returns_a_plain_preview():
    update_secrets(groww_api_key="super-secret-key", groww_api_secret="super-secret")
    status = secret_status()
    assert status["groww_api_key"] is True
    assert "super-secret-key" not in str(status["preview"])


def test_strategy_change_requires_confirmation():
    save_settings({"max_abs_move": 4})
    report = readiness(load_settings(), "abc", ["RELIANCE"], False)
    assert report["ready"] is False
    assert any(item["name"] == "Strategy confirmation" and not item["passed"] for item in report["checks"])


def test_live_readiness_passes_only_with_mandatory_approvals(monkeypatch):
    update_secrets(
        groww_api_key="key",
        groww_api_secret="secret",
        firebase_service_account={"type": "service_account", "private_key": "hidden"},
    )
    save_settings({
        "last_groww_test": {"ok": True, "message": "ok"},
        "last_firebase_test": {"ok": True, "message": "ok"},
        "nifty50_approved": True,
        "fno_universe_approved": True,
        "fno_universe_hash": "abc",
        "sector_enabled": False,
        "sector_choice_saved": True,
    })
    report = readiness(load_settings(), "abc", ["RELIANCE"], False)
    assert report["ready"] is True
    monkeypatch.delenv("NSE_FNO_SCANNER_HOME", raising=False)


def test_candidate_selection_merges_sources_and_blocks_unapproved_oi():
    settings = load_settings()
    market = {"status": "BULLISH", "direction": "LONG", "complete": True}
    rows = [{"symbol": "AAA", "name": "A", "change_pct": 1.2}, {"symbol": "BBB", "name": "B", "change_pct": 0.4}]
    chosen, notes = choose_candidates(settings, market, rows)
    assert {row["symbol"] for row in chosen} == {"AAA", "BBB"}
    assert "client clarification" in notes["oi"].lower()
    assert "nse" in notes["nse_rank"].lower()
    assert all("Method 3" not in row["sources"] for row in chosen)


def test_early_candles_create_one_live_signal_and_do_not_repeat():
    now = datetime(2026, 11, 20, 9, 25)
    cache = empty_cache("2026-11-20")
    payload = {"candles": [
        ["2026-11-20T09:15:00", 100, 102, 99, 101, 1000, None],
        ["2026-11-20T09:20:00", 101, 101.5, 100, 100.4, 900, None],
    ]}
    merge_candles(cache, "RELIANCE", parse_groww_candles(payload))
    assert store_previous_day(cache, "RELIANCE", [
        {"ts": "2026-11-19 00:00:00", "open": 90, "high": 110, "low": 80, "close": 100, "volume": 1}
    ], "2026-11-20")
    settings = load_settings()
    candidate = {"symbol": "RELIANCE", "name": "Reliance", "ltp": 101, "change_pct": 1, "sources": ["Top movers"]}
    first, _note = signals_for_candidate(cache, candidate, "LONG", now.date(), now, settings, set())
    assert len(first) == 1
    assert first[0]["origin"] == "live"
    assert first[0]["setup"] == "SETUP 1"
    second, _note = signals_for_candidate(cache, candidate, "LONG", now.date(), now, settings, {first[0]["id"]})
    assert second == []


def test_quotes_without_a_previous_close_are_unusable():
    now = datetime(2026, 11, 20, 9, 20)
    quotes = quotes_from_prices(
        [{"symbol": "RELIANCE", "name": "Reliance"}],
        {"RELIANCE": (101, now)},
        {},
        now,
    )
    assert quotes["RELIANCE"]["prev_close"] is None


def test_historical_results_are_not_marked_live():
    candles = [
        Candle(datetime(2026, 11, 20, 9, 15), 100, 102, 99, 101, 1000),
        Candle(datetime(2026, 11, 20, 9, 20), 101, 101.5, 100, 100.4, 900),
    ]
    result = run_historical(60, 40, 60, 40, candles, "LONG", 200, 50, 1)
    assert result["live"] is False
    assert result["origin"] == "historical"
    assert result["alerts"]


def test_candle_with_missing_open_is_skipped_instead_of_crashing():
    from nse_scanner.candles import candles_for_symbol, empty_cache, merge_candles
    cache = empty_cache("2026-11-23")
    merge_candles(cache, "INDUSINDBK", [{
        "ts": "2026-11-23 09:15:00",
        "open": None,
        "high": 10,
        "low": 9,
        "close": 9.5,
        "volume": 100,
    }])
    assert candles_for_symbol(cache, "INDUSINDBK", datetime(2026, 11, 23, 10, 0), 5) == []


def test_daily_candle_keeps_high_low_and_close_when_open_is_missing():
    rows = parse_groww_candles({"candles": [["2026-11-20T00:00:00", None, 110, 90, 100, 5, None]]})
    assert rows[0]["close"] == 100
    assert rows[0]["high"] == 110
    assert rows[0]["low"] == 90
    assert rows[0]["open"] is None


def test_source_does_not_place_orders(project_root):
    for path in (project_root / "nse_scanner").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "place_order(" not in text
        assert "modify_order(" not in text
        assert "cancel_order(" not in text
