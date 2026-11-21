"""Decision helpers used by the background scanner.

These functions do not fetch data and do not create a price when one is missing.
"""

from __future__ import annotations

from datetime import datetime, time

from nse_scanner.candles import candles_for_symbol, history_is_complete
from nse_scanner.providers import NSE_RANKING_BLOCK, OI_STATUS, select_nse_intersection
from scanner_rules import (
    change_pct,
    eligible,
    merge_candidates,
    rank_rows,
    explain_setups,
    select_price_leaders,
    select_sector_stocks,
    select_sectors,
    signal_document_id,
    snapshot_is_lockable,
)


def parse_clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def quotes_from_prices(symbols: list[dict], prices: dict, previous: dict, now: datetime) -> dict:
    quotes = {}
    for item in symbols:
        symbol = item["symbol"]
        price = prices.get(symbol)
        prior = previous.get(symbol)
        if price is None or not prior or prior.get("close") in (None, 0):
            quotes[symbol] = {"ltp": None, "prev_close": None, "name": item.get("name") or symbol}
            continue
        ltp, as_of = price
        quotes[symbol] = {
            "ltp": ltp,
            "prev_close": prior["close"],
            "as_of": as_of,
            "name": item.get("name") or symbol,
        }
    return quotes


def priced_rows(group: dict) -> list[dict]:
    rows = []
    for item in group.get("priced") or []:
        pct = change_pct(item["ltp"], item["prev_close"])
        if pct is None:
            continue
        rows.append({
            "symbol": item["symbol"],
            "name": item.get("name") or item["symbol"],
            "ltp": item["ltp"],
            "prev_close": item["prev_close"],
            "change_pct": pct,
            "as_of": item.get("as_of"),
        })
    return rows


def choose_candidates(settings: dict, market: dict, fno_rows: list[dict], sector_moves: list[dict] | None = None,
                      sector_members: dict | None = None, oi_rows: list[dict] | None = None) -> tuple[list, dict]:
    notes = {}
    if market.get("status") not in ("BULLISH", "BEARISH") or not market.get("direction"):
        return [], {"selection": market.get("reason") or "Market gate is closed."}
    direction = market["direction"]
    status = market["status"]
    limit = int(settings.get("top_n") or 5)
    max_move = float(settings.get("max_abs_move") or 5)
    tie = bool(settings.get("tie_break_approved"))
    picked = {}
    if settings.get("price_movers_enabled", True):
        rows, error = select_price_leaders(fno_rows, direction, status, limit, max_move, tie)
        notes["top_movers"] = error or (
            "Method 1 uses the written rule: top 5 F&O price gainers when the market is bullish, "
            "or top 5 F&O price losers when it is bearish. The heading says OI Spurts, "
            "but no OI-spike threshold was defined, so open interest is not used to rank these stocks."
        )
        if not error:
            picked["Method 1"] = _with_ranks(rows, "Method 1")
    nse_rows, nse_note = select_nse_intersection(
        settings.get("nse_ranking_rows"), fno_rows, direction, status, limit, max_move,
    )
    notes["nse_rank"] = nse_note or NSE_RANKING_BLOCK
    if nse_rows:
        picked["Method 3"] = _with_ranks(nse_rows, "Method 3")
    if settings.get("sector_block"):
        notes["sector"] = settings["sector_block"]
    elif settings.get("sector_enabled"):
        sector_rows, error = _sector_picks(settings, direction, status, sector_moves or [], sector_members or {}, limit, max_move, tie)
        notes["sector"] = error or "Sector method produced its eligible stocks."
        if not error:
            picked["Method 2"] = sector_rows
    else:
        notes["sector"] = "Sector selection is turned off."
    notes["oi"] = OI_STATUS
    del oi_rows
    return merge_candidates(picked), notes


def _with_ranks(rows: list[dict], method: str) -> list[dict]:
    ranked = []
    for index, row in enumerate(rows, start=1):
        copied = dict(row)
        copied["rank_label"] = f"{method} #{index}"
        copied["selection_rank"] = index
        ranked.append(copied)
    return ranked


def _sector_picks(settings, direction, status, sector_moves, sector_members, limit, max_move, tie):
    official = settings.get("sector_source") == "official_constituents"
    if not official and settings.get("sector_method") == "constituent_average" and not settings.get("sector_constituents_approved"):
        return [], "Constituent-average sector ranking is not approved."
    if not official and settings.get("sector_method") != "constituent_average" and not settings.get("sector_method_approved"):
        return [], "Official sector-index ranking is not approved."
    count = int(settings.get("sector_count") or 2)
    per_sector = int(settings.get("stocks_per_sector") or 5)
    chosen, error = select_sectors(sector_moves, direction, count, tie)
    if error:
        return [], error
    collected = []
    for sector in chosen:
        name = sector.get("sector") or sector.get("symbol")
        members = sector_members.get(name) or []
        if not members:
            return [], f"No verified F&O constituents are available for {name}."
        stocks, stock_error = select_sector_stocks(members, direction, status, per_sector, max_move, tie)
        if stock_error:
            return [], stock_error
        for index, row in enumerate(stocks, start=1):
            copied = dict(row)
            copied["sector"] = name
            copied["selection_rank"] = index
            copied["rank_label"] = f"Method 2 {name} #{index}"
            collected.append(copied)
    return collected, None


def reversal_allows_signals(lock_direction: str | None, market: dict | None, protection_enabled: bool) -> tuple[bool, str]:
    """Keep the current rule: protection is off unless both approval flags are set.

    The 9:30 list is never rebuilt from later prices. Whether a later breadth
    change should stop new signals still needs a client decision.
    """
    if not lock_direction:
        return False, "No direction was locked, so no signal was generated."
    market = market or {}
    if not protection_enabled:
        return True, (
            "NEEDS CLIENT CONFIRMATION. The locked list is still scanned after the live breadth changes. "
            "The 9:30 list is not rebuilt."
        )
    if market.get("complete") and market.get("direction") == lock_direction:
        return True, "Live breadth still matches the locked direction."
    return False, "Breadth-reversal protection is on. Live breadth does not match the locked direction, so no new signal was generated."


def effective_setup3_inclusion(settings: dict) -> bool:
    """The client rule excludes a candle that starts at 11:00. Including it needs an extra approval."""
    return bool(settings.get("setup3_include_cutoff_candle") and settings.get("setup3_include_1100_approved"))


def effective_reversal_protection(settings: dict) -> bool:
    """The locked 9:30 list stays fixed. A later breadth change does not cancel setups unless this extra protection is approved."""
    return bool(settings.get("breadth_reversal_protection") and settings.get("breadth_reversal_protection_change_approved"))


def lock_from_snapshot(snapshot: dict | None, now: datetime, deadline: time, max_age_seconds: float) -> tuple[dict | None, str]:
    if not snapshot:
        return None, "No complete breadth snapshot was available at or before the selection deadline."
    taken = snapshot.get("taken_at")
    if not isinstance(taken, datetime):
        return None, "The breadth snapshot has no timestamp, so it was not locked."
    if not snapshot_is_lockable(taken, now, deadline, max_age_seconds):
        return None, "The last breadth snapshot was taken after the deadline or it was stale."
    if not snapshot.get("market", {}).get("complete"):
        return None, "The breadth snapshot is incomplete, so no candidates were locked."
    return snapshot, "Candidate list locked from the last fresh snapshot at or before 9:30."


def signals_for_candidate(cache: dict, candidate: dict, direction: str, trading_date, now: datetime, settings: dict,
                          known_ids: set[str]) -> tuple[list, str | None]:
    symbol = candidate["symbol"]
    complete, missing = history_is_complete(
        cache, symbol, trading_date, now,
        open_time=parse_clock(settings["market_open"]),
        close_time=parse_clock(settings["market_close"]),
        interval_minutes=int(settings["candle_minutes"]),
    )
    if not complete:
        candidate["setup_checks"] = [{
            "label": "Completed 5-minute candles",
            "actual": f"{len(missing)} completed session candles are missing",
            "required": "Every completed session slot before a setup is evaluated",
            "result": "BLOCKED",
        }]
        return [], f"{symbol} is missing {len(missing)} completed candles, so no setup was evaluated."
    candles = candles_for_symbol(cache, symbol, now, int(settings["candle_minutes"]))
    previous = (cache.get("previous") or {}).get(symbol)
    explained = explain_setups(
        candles,
        direction,
        None if not previous else float(previous["high"]),
        None if not previous else float(previous["low"]),
        setup3_cutoff=parse_clock(settings["setup3_end"]),
        max_combined_pct=float(settings["setup2_combined_pct"]),
        max_second_pct=float(settings["setup2_second_pct"]),
        include_cutoff_candle=effective_setup3_inclusion(settings),
    )
    candidate["setup_checks"] = explained["checks"]
    alerts = explained["alerts"]
    fresh = []
    for alert in alerts:
        document_id = signal_document_id(str(trading_date), symbol, alert["setup"], alert["candle_time"], direction)
        if document_id in known_ids:
            continue
        fresh.append({
            "id": document_id,
            "origin": "live",
            "trading_date": str(trading_date),
            "symbol": symbol,
            "name": candidate.get("name") or symbol,
            "direction": direction,
            "setup": alert["setup"],
            "candle_time": alert["candle_time"],
            "open": alert["open"],
            "high": alert["high"],
            "low": alert["low"],
            "close": alert["close"],
            "volume": alert["volume"],
            "trigger_close": alert["trigger_close"],
            "detail": alert["detail"],
            "checks": alert.get("checks") or [],
            "previous_day_high": None if not previous else previous.get("high"),
            "previous_day_low": None if not previous else previous.get("low"),
            "selection_price": candidate.get("ltp"),
            "change_pct": candidate.get("change_pct"),
            "sector": candidate.get("sector") or "",
            "sources": candidate.get("sources") or [],
            "market_status": "BULLISH" if direction == "LONG" else "BEARISH",
        })
    return fresh, None if previous else f"{symbol} has no previous-day candle, so Setup 2 was not evaluated."
