"""Derive the F&O equity universe from a Groww instrument master.

A symbol is included only when an NSE F&O contract names it as the underlying
and the same symbol exists as an NSE cash equity in series EQ. Index
underlyings such as NIFTY do not have that cash series, so they are left out.
"""

from __future__ import annotations

import hashlib

import pandas as pd


def _text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def stable_hash(symbols: list[str]) -> str:
    payload = "\n".join(sorted(set(symbols)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_symbol_csv(path) -> list[str]:
    frame = pd.read_csv(path)
    if "symbol" not in frame.columns:
        raise ValueError("The CSV must contain a symbol column.")
    symbols = []
    for value in frame["symbol"].tolist():
        text = _text(value).upper()
        if text and text not in symbols:
            symbols.append(text)
    return symbols


def load_sector_csv(path) -> dict[str, list[str]]:
    frame = pd.read_csv(path)
    if not {"sector", "symbol"}.issubset(frame.columns):
        raise ValueError("The CSV must contain sector and symbol columns.")
    grouped: dict[str, list[str]] = {}
    for row in frame.itertuples(index=False):
        sector = _text(getattr(row, "sector"))
        symbol = _text(getattr(row, "symbol")).upper()
        if not sector or not symbol:
            continue
        grouped.setdefault(sector, [])
        if symbol not in grouped[sector]:
            grouped[sector].append(symbol)
    return grouped


def cash_equities(frame: pd.DataFrame) -> dict[str, dict]:
    required = {"exchange", "segment", "trading_symbol", "series", "instrument_type"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Instrument master is missing columns: {', '.join(sorted(missing))}.")
    cash = frame[
        (frame["exchange"].map(_text) == "NSE")
        & (frame["segment"].map(_text) == "CASH")
        & (frame["series"].map(_text).str.upper() == "EQ")
        & (frame["instrument_type"].map(_text).str.upper() == "EQ")
    ]
    found = {}
    for row in cash.to_dict(orient="records"):
        symbol = _text(row.get("trading_symbol")).upper()
        if symbol and symbol not in found:
            found[symbol] = {
                "symbol": symbol,
                "name": _text(row.get("name")) or symbol,
                "exchange_token": _text(row.get("exchange_token")),
                "groww_symbol": _text(row.get("groww_symbol")) or f"NSE-{symbol}",
                "isin": _text(row.get("isin")),
                "segment": "CASH",
            }
    return found


def derive_fno_equity_universe(frame: pd.DataFrame) -> list[dict]:
    required = {"exchange", "segment", "trading_symbol", "series", "instrument_type", "underlying_symbol"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Instrument master is missing columns: {', '.join(sorted(missing))}.")
    cash = frame[
        (frame["exchange"].map(_text) == "NSE")
        & (frame["segment"].map(_text) == "CASH")
        & (frame["series"].map(_text).str.upper() == "EQ")
        & (frame["instrument_type"].map(_text).str.upper() == "EQ")
    ].copy()
    cash_by_symbol = {}
    for row in cash.to_dict(orient="records"):
        symbol = _text(row.get("trading_symbol")).upper()
        if symbol and symbol not in cash_by_symbol:
            cash_by_symbol[symbol] = row
    fno = frame[(frame["exchange"].map(_text) == "NSE") & (frame["segment"].map(_text) == "FNO")]
    underlyings = []
    seen = set()
    for value in fno["underlying_symbol"].tolist():
        symbol = _text(value).upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            underlyings.append(symbol)
    stocks = []
    for symbol in underlyings:
        cash_row = cash_by_symbol.get(symbol)
        if not cash_row:
            continue
        stocks.append({
            "symbol": symbol,
            "name": _text(cash_row.get("name")) or symbol,
            "exchange_token": _text(cash_row.get("exchange_token")),
            "groww_symbol": _text(cash_row.get("groww_symbol")) or f"NSE-{symbol}",
            "isin": _text(cash_row.get("isin")),
            "segment": "CASH",
        })
    return stocks


def nearest_futures(frame: pd.DataFrame, underlyings: list[str], trading_date: str) -> dict[str, dict]:
    """Map each underlying to its nearest NSE future, using instrument_type FUT only."""
    if "instrument_type" not in frame.columns:
        return {}
    futures = frame[
        (frame["exchange"].map(_text) == "NSE")
        & (frame["segment"].map(_text) == "FNO")
        & (frame["instrument_type"].map(_text).str.upper() == "FUT")
    ]
    chosen = {}
    for underlying in underlyings:
        rows = futures[futures["underlying_symbol"].map(lambda value: _text(value).upper()) == underlying]
        best = None
        for row in rows.to_dict(orient="records"):
            expiry = _text(row.get("expiry_date"))
            if not expiry or expiry < trading_date:
                continue
            if best is None or expiry < best["expiry_date"]:
                best = {
                    "symbol": underlying,
                    "trading_symbol": _text(row.get("trading_symbol")),
                    "groww_symbol": _text(row.get("groww_symbol")),
                    "exchange_token": _text(row.get("exchange_token")),
                    "expiry_date": expiry,
                }
        if best and best["trading_symbol"]:
            chosen[underlying] = best
    return chosen


def find_index_matches(frame: pd.DataFrame, search_keys: dict[str, tuple[str, ...]]) -> dict[str, list[dict]]:
    """Exact trading-symbol matches only. No fuzzy name mapping."""
    cash = frame[(frame["exchange"].map(_text) == "NSE") & (frame["segment"].map(_text) == "CASH")]
    by_symbol = {}
    for row in cash.to_dict(orient="records"):
        symbol = _text(row.get("trading_symbol")).upper()
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = row
    found = {}
    for sector, keys in search_keys.items():
        matches = []
        for key in keys:
            row = by_symbol.get(key.upper())
            if row:
                matches.append({
                    "sector": sector,
                    "trading_symbol": _text(row.get("trading_symbol")).upper(),
                    "name": _text(row.get("name")),
                    "groww_symbol": _text(row.get("groww_symbol")),
                    "exchange_token": _text(row.get("exchange_token")),
                })
        found[sector] = matches
    return found
