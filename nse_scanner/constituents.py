"""Official Nifty Indices constituent lists.

Symbols come from the NSE archives CSV for that index. A failed download does
not replace the last verified list. An old or invalid list is not used.
"""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.request
from datetime import datetime

from nse_scanner.paths import lists_dir
from nse_scanner.redact import public_text
from nse_scanner.universe import stable_hash
from scanner_rules import SECTOR_NAMES

NIFTY50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
SECTOR_URLS = {
    "Realty": "https://nsearchives.nseindia.com/content/indices/ind_niftyrealtylist.csv",
    "FMCG": "https://nsearchives.nseindia.com/content/indices/ind_niftyfmcglist.csv",
    "PSU Bank": "https://nsearchives.nseindia.com/content/indices/ind_niftypsubanklist.csv",
    "Financial Services": "https://nsearchives.nseindia.com/content/indices/ind_niftyfinancelist.csv",
    "Pharma": "https://nsearchives.nseindia.com/content/indices/ind_niftypharmalist.csv",
    "Bank": "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "Auto": "https://nsearchives.nseindia.com/content/indices/ind_niftyautolist.csv",
    "IT": "https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv",
    "Metal": "https://nsearchives.nseindia.com/content/indices/ind_niftymetallist.csv",
    "Energy": "https://nsearchives.nseindia.com/content/indices/ind_niftyenergylist.csv",
    "Media": "https://nsearchives.nseindia.com/content/indices/ind_niftymedialist.csv",
    "Mid Select": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcapselect_list.csv",
    "India Defence": "https://nsearchives.nseindia.com/content/indices/ind_niftyindiadefence_list.csv",
}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9&.\-]+$")
CACHE_NAME = "official_constituents.json"


def cache_path():
    return lists_dir() / CACHE_NAME


def empty_cache() -> dict:
    return {"nifty50": None, "manual_nifty50": None, "sectors": {}, "manual_sectors": {}}


def load_constituent_cache() -> dict:
    path = cache_path()
    if not path.exists():
        return empty_cache()
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_cache()
    if not isinstance(stored, dict):
        return empty_cache()
    cache = empty_cache()
    cache.update(stored)
    cache["sectors"] = stored.get("sectors") or {}
    cache["manual_sectors"] = stored.get("manual_sectors") or {}
    return cache


def save_constituent_cache(cache: dict) -> None:
    path = cache_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    temporary.replace(path)


def fetch_official_csv(url: str) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            text = response.read().decode("utf-8-sig")
    except Exception as exc:
        return False, public_text(str(exc))
    if not text.strip():
        return False, "The NSE file was empty."
    header = text.splitlines()[0]
    if "Symbol" not in header:
        return False, "The NSE file has no Symbol column, so it was rejected."
    return True, text


def parse_constituent_csv(text: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = [name.strip() for name in (reader.fieldnames or [])]
    symbol_field = next((name for name in fields if name.lower() == "symbol"), None)
    if symbol_field is None:
        raise ValueError("The CSV must contain a Symbol column.")
    series_field = next((name for name in fields if name.lower() == "series"), None)
    symbols = []
    for row in reader:
        if series_field and (row.get(series_field) or "").strip().upper() not in ("", "EQ"):
            continue
        symbol = (row.get(symbol_field) or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def validate_symbols(symbols: list[str], exact_count: int | None = None, minimum: int = 1) -> tuple[bool, str]:
    invalid = [symbol for symbol in symbols if not SYMBOL_PATTERN.fullmatch(symbol)]
    if invalid:
        return False, f"Invalid symbols were rejected: {', '.join(invalid[:8])}."
    if exact_count is not None and len(symbols) != exact_count:
        return False, f"Expected {exact_count} symbols and found {len(symbols)}. The list was rejected."
    if len(symbols) < minimum:
        return False, f"Expected at least {minimum} symbols and found {len(symbols)}. The list was rejected."
    return True, f"{len(symbols)} symbols."


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _age_hours(stamp: str, now: datetime) -> float:
    taken = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    return (now - taken).total_seconds() / 3600


def _entry(symbols: list[str], url: str, now: datetime, previous: dict | None, origin: str) -> dict:
    added, removed = [], []
    if previous and previous.get("symbols"):
        added = sorted(set(symbols) - set(previous["symbols"]))
        removed = sorted(set(previous["symbols"]) - set(symbols))
    return {
        "source_url": url,
        "fetched_at": _stamp(now),
        "symbols": symbols,
        "count": len(symbols),
        "hash": stable_hash(symbols),
        "previous_hash": (previous or {}).get("hash") or "",
        "added": added,
        "removed": removed,
        "changed": bool(previous and (added or removed)),
        "valid": True,
        "error": "",
        "last_error": "",
        "origin": origin,
    }


def _keep_failed(previous: dict | None, error: str, now: datetime) -> dict:
    kept = dict(previous or {})
    kept["valid"] = bool(kept.get("symbols"))
    kept["last_error"] = error
    kept["last_attempt_at"] = _stamp(now)
    kept["error"] = error
    return kept


def list_is_usable(entry: dict | None, now: datetime, max_age_hours: float) -> tuple[bool, str]:
    if not entry or not entry.get("valid") or not entry.get("symbols") or not entry.get("fetched_at"):
        detail = (entry or {}).get("error") or "No verified list is stored."
        return False, detail
    age = _age_hours(entry["fetched_at"], now)
    if age > max_age_hours:
        return False, (
            f"The last successful list from {entry['fetched_at']} is older than {max_age_hours:g} hours. It was not used."
        )
    change = ""
    if entry.get("changed"):
        change = (
            f" Constituents changed. Added: {', '.join(entry.get('added') or []) or 'none'}."
            f" Removed: {', '.join(entry.get('removed') or []) or 'none'}."
        )
    if entry.get("last_error"):
        return True, (
            f"Using the last successful {entry.get('origin') or 'list'} from {entry['fetched_at']} "
            f"because the latest refresh failed: {entry['last_error']}.{change}"
        )
    return True, f"{entry.get('origin') or 'list'} from {entry['fetched_at']}, {entry.get('count')} symbols.{change}"


def _due(entry: dict | None, now: datetime, refresh_hours: float, force: bool) -> bool:
    if force or not entry or not entry.get("valid") or not entry.get("fetched_at"):
        return True
    return _age_hours(entry["fetched_at"], now) >= refresh_hours


def refresh_official_lists(now: datetime, fetcher=None, max_age_hours: float = 168,
                           refresh_hours: float = 12, force: bool = False) -> dict:
    fetcher = fetcher or fetch_official_csv
    cache = load_constituent_cache()
    if _due(cache.get("nifty50"), now, refresh_hours, force):
        cache["nifty50"] = _refresh_one(cache.get("nifty50"), NIFTY50_URL, fetcher, now, exact_count=50)
    sectors = dict(cache.get("sectors") or {})
    for name in SECTOR_NAMES:
        url = SECTOR_URLS[name]
        if _due(sectors.get(name), now, refresh_hours, force):
            sectors[name] = _refresh_one(sectors.get(name), url, fetcher, now, minimum=5)
    cache["sectors"] = sectors
    save_constituent_cache(cache)
    return describe_lists(cache, now, max_age_hours)


def _refresh_one(previous, url, fetcher, now, exact_count=None, minimum=1) -> dict:
    ok, payload = fetcher(url)
    if not ok:
        return _keep_failed(previous, payload, now)
    try:
        symbols = parse_constituent_csv(payload)
    except ValueError as exc:
        return _keep_failed(previous, str(exc), now)
    valid, detail = validate_symbols(symbols, exact_count=exact_count, minimum=minimum)
    if not valid:
        failed = _keep_failed(previous, detail, now)
        failed["rejected_count"] = len(symbols)
        return failed
    entry = _entry(symbols, url, now, previous if previous and previous.get("valid") else None, "nse_official_csv")
    entry["validation"] = detail
    return entry


def save_manual_nifty(symbols: list[str], now: datetime) -> tuple[bool, str]:
    cleaned = []
    for symbol in symbols:
        text = str(symbol or "").strip().upper()
        if text and text not in cleaned:
            cleaned.append(text)
    valid, detail = validate_symbols(cleaned, exact_count=50)
    cache = load_constituent_cache()
    if not valid:
        cache["manual_nifty50"] = {
            "valid": False,
            "symbols": [],
            "error": detail,
            "fetched_at": _stamp(now),
            "origin": "manual_csv",
        }
        save_constituent_cache(cache)
        return False, detail
    cache["manual_nifty50"] = _entry(cleaned, "manual_csv", now, cache.get("manual_nifty50"), "manual_csv")
    save_constituent_cache(cache)
    return True, "Manual Nifty 50 CSV saved as the fallback list."


def save_manual_sectors(mapping: dict[str, list[str]], now: datetime) -> tuple[bool, str]:
    cleaned = {}
    for name in SECTOR_NAMES:
        symbols = []
        for symbol in mapping.get(name) or []:
            text = str(symbol or "").strip().upper()
            if text and text not in symbols:
                symbols.append(text)
        if symbols:
            valid, detail = validate_symbols(symbols, minimum=1)
            if not valid:
                return False, f"{name}: {detail}"
            cleaned[name] = symbols
    missing = [name for name in SECTOR_NAMES if name not in cleaned]
    cache = load_constituent_cache()
    cache["manual_sectors"] = {
        "fetched_at": _stamp(now),
        "sectors": cleaned,
        "origin": "manual_csv",
        "valid": not missing,
        "error": "" if not missing else f"Manual sector file is missing: {', '.join(missing)}.",
    }
    save_constituent_cache(cache)
    if missing:
        return False, cache["manual_sectors"]["error"]
    return True, "Manual sector CSV saved as the fallback list."


def active_nifty(cache: dict, now: datetime, max_age_hours: float) -> tuple[list[str], str, str]:
    official_ok, official_detail = list_is_usable(cache.get("nifty50"), now, max_age_hours)
    if official_ok:
        entry = cache["nifty50"]
        return list(entry["symbols"]), official_detail, entry.get("origin") or "nse_official_csv"
    manual_ok, manual_detail = list_is_usable(cache.get("manual_nifty50"), now, max_age_hours)
    if manual_ok:
        return list(cache["manual_nifty50"]["symbols"]), (
            f"Official Nifty 50 list is not usable ({official_detail}). Manual CSV fallback: {manual_detail}"
        ), "manual_csv"
    return [], official_detail, "none"


def active_sectors(cache: dict, now: datetime, max_age_hours: float) -> tuple[dict, list[str]]:
    official = cache.get("sectors") or {}
    manual = (cache.get("manual_sectors") or {}).get("sectors") or {}
    manual_entry = cache.get("manual_sectors") or {}
    manual_ok, manual_detail = list_is_usable(
        {"valid": bool(manual), "symbols": ["stored"], "fetched_at": manual_entry.get("fetched_at"), "origin": "manual_csv"}
        if manual_entry.get("fetched_at") else None,
        now,
        max_age_hours,
    )
    selected = {}
    errors = []
    for name in SECTOR_NAMES:
        ok, detail = list_is_usable(official.get(name), now, max_age_hours)
        if ok:
            selected[name] = list(official[name]["symbols"])
            continue
        if manual_ok and manual.get(name):
            selected[name] = list(manual[name])
            errors.append(f"{name}: official list is not usable ({detail}). Manual fallback is in use ({manual_detail}).")
            continue
        errors.append(f"{name}: {detail}")
    return selected, errors


def describe_lists(cache: dict, now: datetime, max_age_hours: float) -> dict:
    symbols, detail, origin = active_nifty(cache, now, max_age_hours)
    sectors, errors = active_sectors(cache, now, max_age_hours)
    nifty_entry = cache.get("nifty50") or {}
    return {
        "nifty50": {
            "usable": bool(symbols),
            "symbols": symbols,
            "count": len(symbols),
            "origin": origin,
            "detail": detail,
            "fetched_at": nifty_entry.get("fetched_at") or "",
            "hash": nifty_entry.get("hash") or "",
            "added": nifty_entry.get("added") or [],
            "removed": nifty_entry.get("removed") or [],
            "source_url": nifty_entry.get("source_url") or NIFTY50_URL,
        },
        "sectors": {
            "ready": len(sectors) == len(SECTOR_NAMES),
            "count": len(sectors),
            "names": list(sectors),
            "errors": errors,
            "detail": "All 13 official sector lists are usable." if not errors else " ".join(errors),
        },
        "sector_symbols": sectors,
    }


def apply_cash_validation(report: dict, cash_symbols: set[str]) -> dict:
    """Drop the Nifty list when any official symbol is absent from Groww cash EQ."""
    nifty = dict(report.get("nifty50") or {})
    symbols = list(nifty.get("symbols") or [])
    missing = [symbol for symbol in symbols if symbol not in cash_symbols]
    if symbols and missing:
        shown = ", ".join(missing[:12])
        nifty.update({
            "usable": False,
            "symbols": [],
            "count": 0,
            "missing_from_groww": missing,
            "detail": (
                f"These Nifty 50 symbols are not NSE cash equities in the Groww master: {shown}. "
                "The list was not used."
            ),
        })
        report = dict(report)
        report["nifty50"] = nifty
    return report
