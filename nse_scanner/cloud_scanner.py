"""Cloud-native button-driven scanning and setup evaluation.

Designed for Streamlit Community Cloud execution without any background workers,
VPS dependencies, systemd/supervisord, or separate FastAPI servers.
All scanning and candle setup evaluations run directly inside Streamlit on user action.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

from nse_scanner.candles import (
    candles_for_symbol,
    empty_cache,
    history_is_complete,
    load_cache,
    merge_candles,
    parse_groww_candles,
    save_cache,
    store_previous_day,
)
from nse_scanner.constituents import (
    active_nifty,
    active_sectors,
    apply_cash_validation,
    refresh_official_lists,
)
from nse_scanner.decisions import build_board
from nse_scanner.engine import (
    choose_candidates,
    parse_clock,
    priced_rows,
    quotes_from_prices,
    signals_for_candidate,
)
from nse_scanner.paths import lists_dir, runtime_dir
from nse_scanner.providers import method_two_block, sector_index_gap
from nse_scanner.redact import public_text
from nse_scanner.settings import developer_mode_enabled, quote_max_age_seconds
from nse_scanner.state import read_json, write_json
from nse_scanner.universe import cash_equities, derive_fno_equity_universe, find_index_matches, stable_hash
from scanner_rules import (
    APPROVED_BREADTH_DENOMINATOR,
    DEFAULT_ADVANCE_MIN,
    DEFAULT_DECLINE_MAX,
    DEFAULT_SETUP3_END,
    IST,
    SECTOR_NAMES,
    assess_quote_group,
    change_pct,
    market_from_groups,
    SECTOR_INDEX_SEARCH_KEYS,
)

logger = logging.getLogger("nse_scanner.cloud")


def ist_now() -> datetime:
    """Current Indian Standard Time without tzinfo, matching project convention."""
    return datetime.now(IST).replace(tzinfo=None)


def check_holiday(trading_date: str) -> str:
    """Check if the given date is an official NSE exchange holiday."""
    path = lists_dir() / "holidays.csv"
    if not path.exists():
        return ""
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return ""
    column = next((name for name in frame.columns if str(name).strip().lower() == "date"), None)
    if column is None:
        return ""
    for value in frame[column].tolist():
        if str(value).strip()[:10] == trading_date:
            return trading_date
    return ""


def validate_market_session(now: datetime, settings: dict) -> tuple[bool, str]:
    """Validate Indian market date and operating window."""
    if settings.get("developer_mode"):
        return True, "Developer mode is active (market schedule bypass enabled)."

    trading_date = str(now.date())
    if now.weekday() >= 5:
        return False, "Weekend. The NSE cash session is closed."

    holiday = check_holiday(trading_date)
    if holiday:
        return False, f"Exchange holiday ({holiday}). The live market is closed."

    open_time = parse_clock(settings.get("market_open", "09:15"))
    close_time = parse_clock(settings.get("market_close", "15:30"))
    now_time = now.time().replace(microsecond=0)

    if now_time < open_time:
        return False, f"Market has not opened yet. Session opens at {open_time.strftime('%H:%M')} IST."

    if now_time >= close_time:
        return False, f"Market is closed for the day (closed at {close_time.strftime('%H:%M')} IST)."

    return True, "Market session is active."


def load_universe_and_constituents(client, settings: dict, now: datetime) -> tuple[list[dict], list[dict], dict, dict, str]:
    """Load and validate the F&O equity universe, Nifty 50 constituents, and sector mapping."""
    path = runtime_dir() / "instruments.csv"
    frame = None
    if path.exists():
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if now - mtime < timedelta(hours=18):
                frame = pd.read_csv(path)
        except Exception:
            frame = None

    if frame is None or len(frame) == 0:
        if client is not None and hasattr(client, "download_instruments"):
            frame = client.download_instruments()
            try:
                frame.to_csv(path, index=False)
            except Exception:
                pass
        else:
            raise RuntimeError("Instrument master file is missing and client cannot download it.")

    fno_universe = derive_fno_equity_universe(frame)
    cash = cash_equities(frame)

    list_report = refresh_official_lists(
        now,
        max_age_hours=float(settings.get("constituent_max_age_hours") or 168),
        refresh_hours=float(settings.get("constituent_refresh_hours") or 12),
    )
    list_report = apply_cash_validation(list_report, set(cash))
    nifty_meta = list_report.get("nifty50") or {}
    nifty_symbols = list(nifty_meta.get("symbols") or [])
    nifty_stocks = [cash[s] for s in nifty_symbols if s in cash]
    sector_map = dict(list_report.get("sector_symbols") or {})

    # Method 2 sector check:
    matches = find_index_matches(frame, SECTOR_INDEX_SEARCH_KEYS)
    index_matches = {}
    for sector in SECTOR_NAMES:
        unique = {m["trading_symbol"]: m for m in matches.get(sector, [])}
        index_matches[sector] = sorted(unique)
    parts_missing = sector_index_gap(index_matches)
    constituents_ready = len(sector_map) == len(SECTOR_NAMES)
    sector_block = method_two_block(parts_missing, constituents_ready)

    return nifty_stocks, fno_universe, cash, sector_map, sector_block


def fill_missing_previous_closes(client, monitored: list[dict], cache: dict, trading_date: str, now: datetime) -> int:
    """Ensure previous day close is available in cache for breadth and ranking calculations."""
    start, end = client.previous_day_window(datetime.combine(now.date(), datetime.min.time()))
    interval = client.daily_interval()
    fetched = 0

    # First check batch OHLC if available
    missing_symbols = [item for item in monitored if item["symbol"] not in cache["previous"]]
    if not missing_symbols:
        return 0

    # Try batch OHLC to avoid many requests
    try:
        symbols_to_query = [f"NSE_{item['symbol']}" for item in missing_symbols]
        ohlc_payload = client.get_ohlc_batch(symbols_to_query)
        for item in missing_symbols:
            key = f"NSE_{item['symbol']}"
            data = ohlc_payload.get(key)
            if isinstance(data, dict):
                ohlc = data.get("ohlc") if isinstance(data.get("ohlc"), dict) else data
                close = ohlc.get("close")
                if close not in (None, 0, ""):
                    cache["previous"][item["symbol"]] = {
                        "date": trading_date,
                        "open": ohlc.get("open"),
                        "high": ohlc.get("high"),
                        "low": ohlc.get("low"),
                        "close": float(close),
                        "volume": ohlc.get("volume"),
                    }
                    fetched += 1
    except Exception:
        pass

    # For any remaining, fetch individual daily candle if still missing
    still_missing = [item for item in missing_symbols if item["symbol"] not in cache["previous"]]
    for item in still_missing[:20]:
        try:
            payload = client.historical_candles(item["groww_symbol"], start, end, interval)
            stored = store_previous_day(cache, item["symbol"], parse_groww_candles(payload), trading_date)
            if stored:
                fetched += 1
        except Exception:
            continue

    if fetched > 0:
        save_cache(cache)
    return fetched


def prepare_candidates_scan(client, store, settings: dict, now: datetime, progress_callback=None) -> dict:
    """Phase A: Prepare Candidates.

    Validates market session, authenticates with Groww, fetches fresh market data,
    calculates market breadth, applies the 3 stock-selection methods and 5% movement filter,
    and returns prepared candidates WITHOUT auto-locking.
    """
    valid, message = validate_market_session(now, settings)
    if not valid:
        return {"valid": False, "error": message, "market": None, "candidates": [], "notes": {}}

    if progress_callback:
        progress_callback(10, "Authenticating with Groww...")

    ok, auth_msg = client.authenticate()
    if not ok:
        return {"valid": False, "error": f"Groww authentication failed: {auth_msg}", "market": None, "candidates": [], "notes": {}}

    if progress_callback:
        progress_callback(25, "Loading market universe and index constituents...")

    nifty_stocks, fno_universe, cash, sector_map, sector_block = load_universe_and_constituents(client, settings, now)

    trading_date = str(now.date())
    cache = load_cache(trading_date)

    monitored_map = {item["symbol"]: item for item in nifty_stocks + fno_universe}
    monitored = list(monitored_map.values())

    if progress_callback:
        progress_callback(45, "Ensuring previous day reference prices...")

    fill_missing_previous_closes(client, monitored, cache, trading_date, now)

    if progress_callback:
        progress_callback(65, "Fetching live market prices...")

    query_symbols = [f"NSE_{item['symbol']}" for item in monitored]
    try:
        ltp_payload = client.get_ltp_batch(query_symbols)
    except Exception as exc:
        return {"valid": False, "error": f"Failed to fetch market prices from Groww: {public_text(str(exc))}", "market": None, "candidates": [], "notes": {}}

    prices = {}
    for key, value in ltp_payload.items():
        if value is None or "_" not in str(key):
            continue
        sym = str(key).split("_", 1)[1]
        try:
            prices[sym] = (float(value), now)
        except (ValueError, TypeError):
            continue

    if progress_callback:
        progress_callback(80, "Calculating market breadth (60/40 rule)...")

    max_age = quote_max_age_seconds(settings)
    thresholds = {
        "advance_min": float(settings.get("advance_min", DEFAULT_ADVANCE_MIN)),
        "decline_max": float(settings.get("decline_max", DEFAULT_DECLINE_MAX)),
        "denominator": settings.get("breadth_denominator", APPROVED_BREADTH_DENOMINATOR),
    }

    nifty_quotes = quotes_from_prices(nifty_stocks, prices, cache.get("previous", {}), now)
    fno_quotes = quotes_from_prices(fno_universe, prices, cache.get("previous", {}), now)

    nifty_group = assess_quote_group(nifty_quotes, [s["symbol"] for s in nifty_stocks], now, max_age)
    fno_group = assess_quote_group(fno_quotes, [s["symbol"] for s in fno_universe], now, max_age)

    market = market_from_groups(nifty_group, fno_group, **thresholds)
    market["taken_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

    if progress_callback:
        progress_callback(90, "Evaluating stock selection methods...")

    fno_rows = priced_rows(fno_group)

    working_settings = dict(settings)
    if sector_block:
        working_settings["sector_block"] = sector_block

    candidates, notes = choose_candidates(
        working_settings, market, fno_rows, sector_moves=[], sector_members=None, oi_rows=None
    )

    # Attach live price freshness and names
    for c in candidates:
        sym = c.get("symbol")
        p = prices.get(sym)
        c["current_price"] = p[0] if p else c.get("ltp")
        c["current_price_status"] = "live" if p else "unavailable"
        c["data_updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
        c["name"] = (monitored_map.get(sym) or {}).get("name", sym)

    if progress_callback:
        progress_callback(100, "Candidate preparation complete.")

    return {
        "valid": True,
        "error": "",
        "trading_date": trading_date,
        "taken_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "candidates": candidates,
        "considered": fno_rows,
        "notes": notes,
        "locked": False,
    }


def lock_and_save_candidates(store, prepared_data: dict, now: datetime, settings: dict) -> tuple[bool, str, dict]:
    """Phase B: Save Candidates.

    Validates candidate list, verifies that market direction is clear,
    prevents duplicate saves / race conditions, saves to Firestore and local JSON,
    and locks the candidate list for the trading day.
    """
    if not prepared_data or not prepared_data.get("valid"):
        return False, "Cannot save: candidates have not been prepared or preparation failed.", {}

    market = prepared_data.get("market") or {}
    direction = market.get("direction")
    status = market.get("status")

    if status in ("NEUTRAL", "OPPOSITE") or not direction:
        return False, f"Cannot save candidates: Market direction is {status}. Candidates are only tradeable in BULLISH or BEARISH conditions.", {}

    candidates = prepared_data.get("candidates") or []
    if not candidates:
        return False, "Cannot save candidates: No qualifying stocks met the selection criteria and 5% move cap.", {}

    trading_date = prepared_data.get("trading_date") or str(now.date())

    # Check if already locked in Firestore
    if store is not None:
        try:
            ok, existing = store.get_candidates(trading_date)
            if ok and existing and existing.get("locked"):
                return False, f"Candidates for trading day {trading_date} are already locked. A second scan cannot overwrite the locked list.", existing
        except Exception as exc:
            logger.warning("Firestore check failed during lock_and_save: %s", exc)

    # Check local lock
    local_lock = read_json("selection_lock.json", None)
    if local_lock and local_lock.get("trading_date") == trading_date and local_lock.get("locked"):
        return False, f"Candidates for trading day {trading_date} are already locked locally. Overwrite is blocked.", local_lock

    locked_doc = {
        "trading_date": trading_date,
        "locked": True,
        "locked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "direction": direction,
        "market": market,
        "candidates": candidates,
        "considered": prepared_data.get("considered") or [],
        "notes": prepared_data.get("notes") or {},
        "symbols": [c.get("symbol") for c in candidates if c.get("symbol")],
        "origin": "developer" if settings.get("developer_mode") else "live",
        "reason": f"Candidates locked at {now.strftime('%H:%M:%S IST')} on {trading_date}.",
    }

    # Persist in Firestore
    if store is not None:
        try:
            ok, msg = store.save_candidates(locked_doc)
            if not ok:
                logger.warning("Firestore save_candidates returned false: %s", msg)
        except Exception as exc:
            logger.warning("Firestore save_candidates exception: %s", exc)

    # Persist locally in runtime
    write_json("selection_lock.json", locked_doc)

    return True, f"Candidate list for {trading_date} successfully locked and saved to Firestore.", locked_doc


def restore_today_state(store, trading_date: str) -> dict:
    """Phase C helper: Restore today's locked candidates, monitoring state, and signals.

    Ensures remote client (Odisha) and administrator (Maharashtra) see synchronized state.
    """
    locked_doc = None
    monitoring_doc = None
    signals = []

    # Try Firestore first
    if store is not None:
        try:
            ok_cand, cand = store.get_candidates(trading_date)
            if ok_cand and cand and cand.get("locked"):
                locked_doc = cand
        except Exception as exc:
            logger.warning("Firestore restore candidates error: %s", exc)

        try:
            ok_mon, mon = store.get_monitoring_state(trading_date)
            if ok_mon and mon:
                monitoring_doc = mon
        except Exception as exc:
            logger.warning("Firestore restore monitoring state error: %s", exc)

        try:
            ok_sig, sig_list = store.query_signals(trading_date, limit=200, after_id=None)
            if ok_sig and isinstance(sig_list, list):
                signals = [s for s in sig_list if isinstance(s, dict) and "error" not in s]
        except Exception as exc:
            logger.warning("Firestore restore signals error: %s", exc)

    # Fallback to local files if Firestore returned nothing
    if locked_doc is None:
        local_cand = read_json("selection_lock.json", None)
        if local_cand and local_cand.get("trading_date") == trading_date and local_cand.get("locked"):
            locked_doc = local_cand

    return {
        "locked": locked_doc,
        "monitoring_state": monitoring_doc,
        "signals": signals,
    }


def evaluate_monitoring_cycle(client, store, locked_data: dict, now: datetime, settings: dict,
                             cache: dict | None = None, known_ids: set[str] | None = None) -> dict:
    """Phase C & D: Evaluate Setup Monitoring Cycle.

    Fetches completed 5-minute candles for the locked candidate list,
    checks for monitoring gaps, evaluates Setups 1, 2, 3, saves new signals to Firestore,
    updates monitoring state, and produces the complete decision board.
    Stops monitoring automatically at 11:00 AM IST.
    """
    if not locked_data or not locked_data.get("locked"):
        return {"error": "No candidate list is locked. Monitoring cannot start.", "board": None, "signals": []}

    settings = {
        "market_open": "09:15",
        "market_close": "15:30",
        "setup3_end": "11:00",
        "candle_minutes": 5,
        "setup2_combined_pct": 2.5,
        "setup2_second_pct": 1.0,
        "setup3_inclusion_confirmed": True,
        **settings,
    }

    trading_date = locked_data.get("trading_date") or str(now.date())
    direction = locked_data.get("direction")
    candidates = locked_data.get("candidates") or []
    cutoff_time = parse_clock(settings.get("setup3_end", "11:00"))

    if cache is None:
        cache = load_cache(trading_date)
    if known_ids is None:
        known_ids = set()

    is_past_cutoff = now.time().replace(microsecond=0) >= cutoff_time.replace(microsecond=0)

    # Detect monitoring gaps
    last_mon = None
    if store is not None:
        try:
            _ok, last_mon = store.get_monitoring_state(trading_date)
        except Exception:
            pass

    gap_detected = False
    gap_message = ""
    if last_mon and last_mon.get("last_monitored_candle"):
        last_candle_str = last_mon["last_monitored_candle"]
        try:
            last_dt = datetime.strptime(last_candle_str, "%Y-%m-%d %H:%M:%S")
            # Calculate current completed 5-min candle time
            current_completed_minute = (now.minute // 5) * 5
            current_completed_dt = now.replace(minute=current_completed_minute, second=0, microsecond=0)
            if (current_completed_dt - last_dt).total_seconds() > 300:
                gap_detected = True
                gap_message = (
                    f"Monitoring gap detected: App was inactive or restarted. "
                    f"Last evaluated candle was {last_candle_str}, current candle is {current_completed_dt.strftime('%H:%M:%S')}. "
                    f"Resumed evaluation for all completed candles."
                )
        except Exception:
            pass

    # Fetch 5-minute candles for locked candidates if client is available
    if client is not None and not is_past_cutoff:
        try:
            interval = client.five_minute_interval()
            start = datetime.combine(now.date(), parse_clock(settings.get("market_open", "09:15")))
            for item in candidates:
                symbol = item["symbol"]
                groww_symbol = item.get("groww_symbol") or f"NSE-{symbol}"
                try:
                    payload = client.historical_candles(groww_symbol, start, now, interval)
                    rows = [row for row in parse_groww_candles(payload) if row["ts"][:10] == trading_date]
                    merge_candles(cache, symbol, rows)
                except Exception as exc:
                    logger.warning("Candle fetch error for %s: %s", symbol, exc)
            save_cache(cache)
        except Exception as exc:
            logger.warning("Historical interval error: %s", exc)

    # Evaluate setups for each candidate
    new_signals = []
    enriched_candidates = []
    latest_candle_time = ""

    for candidate in candidates:
        symbol = candidate["symbol"]
        signals, note = signals_for_candidate(
            cache, candidate, direction, now.date(), now, settings, known_ids
        )
        for sig in signals:
            sig_id = sig["id"]
            if sig_id not in known_ids:
                known_ids.add(sig_id)
                new_signals.append(sig)
                if store is not None:
                    try:
                        store.save_signal(sig_id, sig)
                    except Exception as exc:
                        logger.warning("Failed to persist signal %s: %s", sig_id, exc)

        # Enrich candidate with current Day High/Low, Volume, Previous Close
        enriched = dict(candidate)
        completed = candles_for_symbol(cache, symbol, now, int(settings.get("candle_minutes", 5)))
        if completed:
            enriched["day_high"] = max(c.high for c in completed)
            enriched["day_low"] = min(c.low for c in completed)
            enriched["volume"] = sum(c.volume for c in completed)
            latest_candle_time = completed[-1].ts.strftime("%Y-%m-%d %H:%M:%S")

        prev = (cache.get("previous") or {}).get(symbol) or {}
        if prev:
            enriched["previous_day_high"] = prev.get("high")
            enriched["previous_day_low"] = prev.get("low")
            enriched["prev_close"] = enriched.get("prev_close") or prev.get("close")
        enriched["in_fno"] = True
        enriched_candidates.append(enriched)

    # Query all accumulated signals for today
    all_signals = list(new_signals)
    if store is not None:
        try:
            ok, fetched_sigs = store.query_signals(trading_date, limit=200, after_id=None)
            if ok and isinstance(fetched_sigs, list):
                sig_map = {s["id"]: s for s in fetched_sigs if isinstance(s, dict) and "id" in s}
                for s in new_signals:
                    sig_map[s["id"]] = s
                all_signals = list(sig_map.values())
        except Exception:
            pass

    # Save monitoring state to Firestore
    state_label = "COMPLETED" if is_past_cutoff else "MONITORING"
    mon_doc = {
        "trading_date": trading_date,
        "state": state_label,
        "last_monitored_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "last_monitored_candle": latest_candle_time,
        "last_data_update": now.strftime("%Y-%m-%d %H:%M:%S"),
        "gap_detected": gap_detected,
        "gap_message": gap_message,
        "signals_count": len(all_signals),
        "cutoff_reached": is_past_cutoff,
    }
    if store is not None:
        try:
            store.save_monitoring_state(trading_date, mon_doc)
        except Exception as exc:
            logger.warning("Failed to save monitoring state: %s", exc)

    # Build decision board
    board = build_board(
        locked_data.get("market") or {},
        enriched_candidates,
        locked_data.get("considered") or [],
        all_signals,
        locked_data.get("notes") or {},
        trading_date,
        data_updated=now.strftime("%Y-%m-%d %H:%M:%S"),
        sector_ready=False,
        locked=True,
    )

    return {
        "board": board,
        "signals": all_signals,
        "new_signals": new_signals,
        "monitoring_state": mon_doc,
        "gap_detected": gap_detected,
        "gap_message": gap_message,
        "is_past_cutoff": is_past_cutoff,
    }
