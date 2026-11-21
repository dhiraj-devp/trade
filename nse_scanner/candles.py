"""In-memory candle cache. Completed candles are kept on disk so a restart
does not drop the 9:15 history. Live ticks are not written here as candles.
"""

from __future__ import annotations

import json
from datetime import datetime

from nse_scanner.paths import runtime_dir
from scanner_rules import Candle, completed_candle, missing_session_slots, parse_candle_time


def _path():
    return runtime_dir() / "candle_cache.json"


def empty_cache(trading_date: str) -> dict:
    return {"trading_date": trading_date, "candles": {}, "previous": {}}


def load_cache(trading_date: str) -> dict:
    path = _path()
    if not path.exists():
        return empty_cache(trading_date)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_cache(trading_date)
    if data.get("trading_date") != trading_date:
        previous = data.get("previous") or {}
        kept = {symbol: row for symbol, row in previous.items() if row.get("date", "") < trading_date}
        return {"trading_date": trading_date, "candles": {}, "previous": kept}
    data.setdefault("candles", {})
    data.setdefault("previous", {})
    return data


def save_cache(cache: dict) -> None:
    path = _path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache), encoding="utf-8")
    temporary.replace(path)


def parse_groww_candles(payload: dict) -> list[dict]:
    rows = []
    for item in payload.get("candles") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        try:
            ts = parse_candle_time(item[0])
            row = {
                "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]) if len(item) > 5 and item[5] is not None else None,
            }
        except (TypeError, ValueError):
            continue
        if row["volume"] is None:
            continue
        rows.append(row)
    return rows


def merge_candles(cache: dict, symbol: str, rows: list[dict]) -> None:
    current = {row["ts"]: row for row in cache["candles"].get(symbol, [])}
    for row in rows:
        current[row["ts"]] = row
    cache["candles"][symbol] = [current[key] for key in sorted(current)]


def store_previous_day(cache: dict, symbol: str, rows: list[dict], trading_date: str) -> bool:
    earlier = [row for row in rows if row["ts"][:10] < trading_date]
    if not earlier:
        return False
    chosen = max(earlier, key=lambda row: row["ts"])
    cache["previous"][symbol] = {
        "date": chosen["ts"][:10],
        "open": chosen["open"],
        "high": chosen["high"],
        "low": chosen["low"],
        "close": chosen["close"],
        "volume": chosen["volume"],
    }
    return True


def candles_for_symbol(cache: dict, symbol: str, now: datetime, interval_minutes: int = 5) -> list[Candle]:
    candles = []
    for row in cache.get("candles", {}).get(symbol, []):
        candle = Candle(
            ts=datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S"),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        if completed_candle(candle, now, interval_minutes):
            candles.append(candle)
    candles.sort(key=lambda candle: candle.ts)
    return candles


def history_is_complete(cache: dict, symbol: str, trading_date, now: datetime, **kwargs) -> tuple[bool, list]:
    candles = candles_for_symbol(cache, symbol, now, kwargs.get("interval_minutes", 5))
    missing = missing_session_slots(candles, trading_date, now, **kwargs)
    return not missing, missing
