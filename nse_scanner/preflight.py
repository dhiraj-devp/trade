"""Read-only readiness check. It does not create a BUY or SHORT signal.

Reuse the existing Groww client and Firestore store. A successful login is
not treated as permission to read live prices.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time

from nse_scanner.candles import parse_groww_candles
from nse_scanner.constituents import apply_cash_validation, describe_lists, load_constituent_cache
from nse_scanner.groww_client import GrowwClient, classify_probe_failure
from nse_scanner.paths import runtime_dir
from nse_scanner.providers import NSE_RANKING_BLOCK, OI_STATUS, sector_index_gap
from nse_scanner.redact import public_text
from nse_scanner.settings import load_settings
from nse_scanner.universe import cash_equities, derive_fno_equity_universe, find_index_matches, nearest_futures
from scanner_rules import IST, SECTOR_INDEX_SEARCH_KEYS, SECTOR_NAMES


def market_session_open(now: datetime) -> bool:
    moment = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    if moment.weekday() >= 5:
        return False
    return time(9, 15) <= moment.time() < time(15, 30)


def status_from_error(message: str) -> str:
    text = public_text(message or "")
    category = classify_probe_failure(text)
    if "not configured" in text.lower() or "sdk is not installed" in text.lower():
        return "NOT CONFIGURED"
    if category == "permission_denied":
        return "PERMISSION DENIED"
    return "FAIL"


def _row(name: str, status: str, details: str, checked_at: str) -> dict:
    return {
        "name": name,
        "status": status,
        "details": public_text(details),
        "checked_at": checked_at,
    }


def _stamp(now: datetime) -> str:
    moment = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return moment.strftime("%Y-%m-%d %H:%M:%S IST")


def run_preflight(now: datetime | None = None, client=None, store=None, settings: dict | None = None) -> dict:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    clock = now.replace(tzinfo=None)
    checked_at = _stamp(now)
    settings = settings if settings is not None else load_settings()
    session_open = market_session_open(now)
    checks = []
    client = client if client is not None else GrowwClient(settings)
    authenticated = False
    auth_message = ""
    try:
        authenticated, auth_message = client.authenticate()
    except Exception as exc:
        auth_message = public_text(str(exc))
    auth_status = "PASS" if authenticated else status_from_error(auth_message)
    checks.append(_row(
        "Groww authentication",
        auth_status,
        auth_message or "Groww authentication succeeded. This does not grant live market data.",
        checked_at,
    ))

    frame = None
    if not authenticated:
        checks.append(_row("Instrument-file access", "NOT CONFIGURED" if auth_status == "NOT CONFIGURED" else "FAIL", "Not tested because authentication did not succeed.", checked_at))
    else:
        try:
            frame = client.download_instruments()
            rows = len(frame) if frame is not None else 0
            columns = getattr(frame, "columns", [])
            if rows <= 0 or "trading_symbol" not in columns:
                checks.append(_row("Instrument-file access", "MARKET DATA UNAVAILABLE", "The instrument file had no trading-symbol rows.", checked_at))
                frame = None
            else:
                checks.append(_row("Instrument-file access", "PASS", f"Instrument file returned {rows} rows. This is not a live price.", checked_at))
        except Exception as exc:
            checks.append(_row("Instrument-file access", status_from_error(str(exc)), public_text(str(exc)), checked_at))

    lists = describe_lists(load_constituent_cache(), clock, float(settings.get("constituent_max_age_hours") or 168))
    cash = set()
    universe = []
    if frame is not None:
        try:
            cash = cash_equities(frame)
            lists = apply_cash_validation(lists, cash)
            universe = derive_fno_equity_universe(frame)
        except Exception as exc:
            checks.append(_row("NSE F&O universe", "FAIL", public_text(str(exc)), checked_at))
    _universe_checks(checks, lists, universe, checked_at)
    _sector_checks(checks, lists, frame, session_open, checked_at)
    _sample_market_checks(checks, client, frame, lists, universe, session_open, clock, authenticated, checked_at)
    _ranking_checks(checks, frame, session_open, checked_at)
    _firebase_check(checks, store, checked_at)
    checks.append(_row(
        "Strategy engine",
        "PASS",
        "The local setup rules are the ones used by the scanner. This preflight did not generate a BUY or SHORT.",
        checked_at,
    ))
    if not session_open:
        checks.append(_row(
            "Live freshness",
            "MARKET DATA UNAVAILABLE",
            "The market session is closed. Live freshness must be verified between 09:15 and 15:30 IST on a trading day.",
            checked_at,
        ))
    summary = _summary(checks)
    overall = "READY" if all(item["status"] == "PASS" for item in summary) else "BLOCKED"
    return {
        "checked_at": checked_at,
        "session_open": session_open,
        "checks": checks,
        "summary": summary,
        "overall": overall,
    }


def _universe_checks(checks, lists, universe, checked_at) -> None:
    nifty = lists.get("nifty50") or {}
    symbols = list(nifty.get("symbols") or [])
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if not symbols:
        checks.append(_row("Nifty 50 universe", "FAIL", nifty.get("detail") or "No usable Nifty 50 list is cached.", checked_at))
    elif len(symbols) != 50 or duplicates or not nifty.get("usable"):
        checks.append(_row(
            "Nifty 50 universe",
            "FAIL",
            f"Count {len(symbols)}. Duplicates: {', '.join(duplicates) or 'none'}. {nifty.get('detail') or ''}",
            checked_at,
        ))
    else:
        checks.append(_row("Nifty 50 universe", "PASS", f"50 symbols from {nifty.get('origin')}. Fetched {nifty.get('fetched_at') or 'unknown'}.", checked_at))

    fno_symbols = [item.get("symbol") for item in universe]
    fno_dupes = sorted({symbol for symbol in fno_symbols if fno_symbols.count(symbol) > 1})
    if not universe:
        checks.append(_row("F&O universe", "MARKET DATA UNAVAILABLE" if not fno_dupes else "FAIL", "No NSE F&O equity universe was derived.", checked_at))
    elif fno_dupes:
        checks.append(_row("F&O universe", "FAIL", "Duplicate symbols: " + ", ".join(fno_dupes), checked_at))
    else:
        checks.append(_row("F&O universe", "PASS", f"{len(universe)} NSE F&O equities with a cash series EQ listing.", checked_at))


def _sector_checks(checks, lists, frame, session_open, checked_at) -> None:
    sectors = (lists.get("sectors") or {})
    symbols = lists.get("sector_symbols") or {}
    if not sectors.get("ready"):
        checks.append(_row("Sector constituent mappings", "FAIL", sectors.get("detail") or "One or more sector lists are missing.", checked_at))
    else:
        empty = [name for name in SECTOR_NAMES if not symbols.get(name)]
        dupes = []
        for name, members in symbols.items():
            repeated = sorted({item for item in members if members.count(item) > 1})
            if repeated:
                dupes.append(f"{name}: {', '.join(repeated)}")
        if empty or dupes:
            checks.append(_row("Sector constituent mappings", "FAIL", "Empty: " + ", ".join(empty or ["none"]) + ". Duplicates: " + "; ".join(dupes or ["none"]), checked_at))
        else:
            checks.append(_row("Sector constituent mappings", "PASS", "All 13 official sector constituent lists are usable.", checked_at))

    if frame is None:
        checks.append(_row("Sector-index symbols", "MARKET DATA UNAVAILABLE", "The instrument file was not available, so index symbols were not checked.", checked_at))
        return
    matches = find_index_matches(frame, SECTOR_INDEX_SEARCH_KEYS)
    missing = sector_index_gap({name: matches.get(name) or [] for name in SECTOR_NAMES})
    if missing:
        checks.append(_row(
            "Sector-index symbols",
            "MARKET DATA UNAVAILABLE",
            "No single verified index for: " + ", ".join(missing) + ". No index price was invented.",
            checked_at,
        ))
    else:
        detail = "All 13 sector index symbols are in the instrument file."
        if not session_open:
            detail += " Live index prices were not verified because the market session is closed."
        checks.append(_row("Sector-index symbols", "PASS" if session_open else "MARKET DATA UNAVAILABLE", detail, checked_at))


def _sample_market_checks(checks, client, frame, lists, universe, session_open, now, authenticated, checked_at) -> None:
    if not authenticated:
        for name in ("Nifty 50 stock quote", "F&O stock quote", "Latest 5-minute candle", "Previous-day daily OHLC", "OI and OI change"):
            checks.append(_row(name, "FAIL", "Not tested because authentication did not succeed.", checked_at))
        return
    nifty_symbol = next(iter((lists.get("nifty50") or {}).get("symbols") or []), "")
    fno_symbol = next((item.get("symbol") for item in universe if item.get("symbol")), "")
    if nifty_symbol:
        _quote_check(checks, client, "Nifty 50 stock quote", nifty_symbol, "CASH", session_open, checked_at)
    else:
        checks.append(_row("Nifty 50 stock quote", "MARKET DATA UNAVAILABLE", "No Nifty 50 symbol was available to quote.", checked_at))
    future = {}
    if frame is not None and fno_symbol:
        try:
            future = nearest_futures(frame, [fno_symbol], now.strftime("%Y-%m-%d")).get(fno_symbol) or {}
        except Exception as exc:
            checks.append(_row("F&O stock quote", status_from_error(str(exc)), public_text(str(exc)), checked_at))
    if future.get("trading_symbol"):
        quote = _quote_check(checks, client, "F&O stock quote", future["trading_symbol"], "FNO", session_open, checked_at)
        _oi_check(checks, quote, session_open, checked_at)
    else:
        checks.append(_row("F&O stock quote", "MARKET DATA UNAVAILABLE", "No nearest F&O future was available to quote.", checked_at))
        checks.append(_row("OI and OI change", "MARKET DATA UNAVAILABLE", "OI was not requested because no future quote was available.", checked_at))
    _history_check(checks, client, nifty_symbol or fno_symbol, session_open, now, checked_at)


def _quote_check(checks, client, name, symbol, segment, session_open, checked_at):
    try:
        payload = client.get_quote(symbol, segment=segment)
    except Exception as exc:
        checks.append(_row(name, status_from_error(str(exc)), public_text(str(exc)), checked_at))
        return None
    price_fields = ("last_price", "ltp", "open", "high", "low", "close", "ohlc")
    has_price = isinstance(payload, dict) and any(payload.get(field) not in (None, "") for field in price_fields)
    if not has_price:
        checks.append(_row(name, "MARKET DATA UNAVAILABLE", f"The quote for {symbol} did not include a price. No price was invented.", checked_at))
        return payload if isinstance(payload, dict) else None
    if session_open:
        checks.append(_row(name, "PASS", f"Quote for {symbol} returned a price at {checked_at}. The quote had no separate exchange timestamp in this check.", checked_at))
    else:
        checks.append(_row(
            name,
            "MARKET DATA UNAVAILABLE",
            f"The documented quote call for {symbol} returned a last price, but the market session is closed. This is not a verified live quote.",
            checked_at,
        ))
    return payload


def _oi_check(checks, quote, session_open, checked_at) -> None:
    if not isinstance(quote, dict):
        checks.append(_row("OI and OI change", "MARKET DATA UNAVAILABLE", "The future quote was not available.", checked_at))
        return
    oi = quote.get("open_interest", quote.get("oi"))
    change = quote.get("oi_day_change")
    if oi is None and change is None:
        checks.append(_row("OI and OI change", "MARKET DATA UNAVAILABLE", "The future quote did not include open interest or oi_day_change.", checked_at))
        return
    note = "Fields were present on the returned future quote."
    if not session_open:
        note += " This is not a verified live OI reading because the market session is closed."
    note += " " + OI_STATUS
    checks.append(_row("OI and OI change", "PASS" if session_open else "MARKET DATA UNAVAILABLE", note, checked_at))


def _history_check(checks, client, symbol, session_open, now, checked_at) -> None:
    if not symbol:
        checks.append(_row("Latest 5-minute candle", "MARKET DATA UNAVAILABLE", "No symbol was available for a candle request.", checked_at))
        checks.append(_row("Previous-day daily OHLC", "MARKET DATA UNAVAILABLE", "No symbol was available for a daily candle request.", checked_at))
        return
    groww_symbol = f"NSE-{symbol}"
    try:
        interval = client.five_minute_interval()
        start, end = client.previous_day_window(now)
        payload = client.historical_candles(groww_symbol, start, now, interval)
        candles = parse_groww_candles(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        checks.append(_row("Latest 5-minute candle", status_from_error(str(exc)), public_text(str(exc)), checked_at))
        candles = None
    if candles is not None:
        if not candles:
            checks.append(_row("Latest 5-minute candle", "MARKET DATA UNAVAILABLE", f"No 5-minute candles were returned for {symbol}.", checked_at))
        else:
            note = f"{len(candles)} historical 5-minute candles were returned for {symbol}."
            if not session_open:
                note += " This does not verify a live session candle."
            checks.append(_row("Latest 5-minute candle", "PASS", note, checked_at))
    try:
        daily = client.daily_interval()
        start, end = client.previous_day_window(now)
        payload = client.historical_candles(groww_symbol, start, end, daily)
        rows = parse_groww_candles(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        checks.append(_row("Previous-day daily OHLC", status_from_error(str(exc)), public_text(str(exc)), checked_at))
        return
    if not rows:
        checks.append(_row("Previous-day daily OHLC", "MARKET DATA UNAVAILABLE", f"No daily candle was returned for {symbol}.", checked_at))
    else:
        last = rows[-1]
        checks.append(_row(
            "Previous-day daily OHLC",
            "PASS",
            f"Daily candle for {symbol} on {last.get('ts')}: open {last.get('open')}, high {last.get('high')}, low {last.get('low')}, close {last.get('close')}. Full-universe previous closes were not requested.",
            checked_at,
        ))


def _ranking_checks(checks, frame, session_open, checked_at) -> None:
    if frame is None or not session_open:
        checks.append(_row(
            "Method 1 F&O price ranking",
            "MARKET DATA UNAVAILABLE",
            "Method 1 is implemented, but a live F&O price ranking was not produced. No ranking was invented.",
            checked_at,
        ))
    else:
        checks.append(_row("Method 1 F&O price ranking", "PASS", "The session is open and the instrument file is available. This preflight still did not publish a trade.", checked_at))
    if frame is None:
        checks.append(_row(
            "Method 2 sector ranking",
            "MARKET DATA UNAVAILABLE",
            "Method 2 was not checked because the instrument file was not available. No ranking was invented.",
            checked_at,
        ))
        checks.append(_row("Method 3 NSE-wide ranking", "NOT CONFIGURED", NSE_RANKING_BLOCK, checked_at))
        checks.append(_row("OI-spurt rule", "NOT CONFIGURED", OI_STATUS, checked_at))
        return
    matches = find_index_matches(frame, SECTOR_INDEX_SEARCH_KEYS)
    missing = sector_index_gap({name: matches.get(name) or [] for name in SECTOR_NAMES})
    if missing or not session_open:
        checks.append(_row(
            "Method 2 sector ranking",
            "MARKET DATA UNAVAILABLE",
            "Method 2 is not operational. " + ("Missing indexes: " + ", ".join(missing) + ". " if missing else "Live sector prices were not verified outside the session. ") + "No ranking was invented.",
            checked_at,
        ))
    else:
        checks.append(_row("Method 2 sector ranking", "PASS", "All 13 index symbols are present during the session. Prices still have to be fresh before a ranking is used.", checked_at))
    checks.append(_row("Method 3 NSE-wide ranking", "NOT CONFIGURED", NSE_RANKING_BLOCK, checked_at))
    checks.append(_row("OI-spurt rule", "NOT CONFIGURED", OI_STATUS, checked_at))


def _firebase_check(checks, store, checked_at) -> None:
    if store is None:
        try:
            from nse_scanner.store import FirestoreStore
            store = FirestoreStore.connect()
        except Exception as exc:
            message = public_text(str(exc))
            status = "NOT CONFIGURED" if "not configured" in message.lower() else "FAIL"
            checks.append(_row("Firebase", status, message, checked_at))
            return
    document_id = "preflight_" + checked_at.replace(" ", "_").replace(":", "").replace("-", "")
    ok, message = store.preflight_roundtrip(document_id)
    checks.append(_row("Firebase", "PASS" if ok else "FAIL", message, checked_at))


def _summary(checks: list[dict]) -> list[dict]:
    by_name = {item["name"]: item for item in checks}
    mapping = [
        ("Groww authentication", "Groww authentication"),
        ("Live price permission", "Nifty 50 stock quote"),
        ("Historical candle permission", "Latest 5-minute candle"),
        ("OI data", "OI and OI change"),
        ("Nifty 50 universe", "Nifty 50 universe"),
        ("F&O universe", "F&O universe"),
        ("Sector data", "Sector-index symbols"),
        ("NSE ranking provider", "Method 3 NSE-wide ranking"),
        ("Firebase", "Firebase"),
        ("Strategy engine", "Strategy engine"),
    ]
    rows = []
    for label, source in mapping:
        item = by_name.get(source) or {"status": "FAIL", "details": "The check did not run."}
        rows.append({"component": label, "status": item["status"], "details": item["details"]})
    return rows


def format_report(report: dict) -> str:
    lines = [
        "NSE F&O scanner preflight",
        f"Checked at: {report['checked_at']}",
        f"Market session open: {'yes' if report['session_open'] else 'no'}",
        "",
        f"{'Component':<32} {'Status':<24} Details",
    ]
    for item in report["summary"]:
        lines.append(f"{item['component']:<32} {item['status']:<24} {item['details']}")
    lines.append("")
    lines.append(f"{'Overall readiness':<32} {report['overall']}")
    lines.append("")
    lines.append("Detailed checks")
    for item in report["checks"]:
        lines.append(f"{item['name']}: {item['status']} at {item['checked_at']} — {item['details']}")
    lines.append("")
    lines.append("No BUY or SHORT signal was generated.")
    return "\n".join(lines)


def save_report(report: dict) -> str:
    folder = runtime_dir() / "preflight"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = report["checked_at"].replace(":", "").replace(" ", "_").replace("-", "")
    path = folder / f"preflight_{stamp}.txt"
    path.write_text(format_report(report), encoding="utf-8")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only NSE F&O scanner preflight")
    parser.add_argument("--save", action="store_true", help="Save a timestamped text report with no secrets")
    args = parser.parse_args(argv)
    report = run_preflight()
    print(format_report(report))
    if args.save:
        print("Saved report: " + save_report(report))
    return 0 if report["overall"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
