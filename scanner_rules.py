"""Approved NSE F&O intraday rules.

Defaults match the existing scanner. Optional arguments exist so an approved
Settings change can be passed in without altering the default strategy.
Unchanged prices are never counted as advances or declines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

DEFAULT_ADVANCE_MIN = 60.0
DEFAULT_DECLINE_MAX = 40.0
DEFAULT_MAX_ABS_MOVE = 5.0
DEFAULT_COMBINED_MOVE = 2.5
DEFAULT_SECOND_MOVE = 1.0
DEFAULT_TOP_N = 5
DEFAULT_SECTOR_COUNT = 2
DEFAULT_STOCKS_PER_SECTOR = 5
DEFAULT_OPEN = time(9, 15)
DEFAULT_SELECTION_DEADLINE = time(9, 30)
DEFAULT_SETUP3_END = time(11, 0)
DEFAULT_CLOSE = time(15, 30)
DEFAULT_CANDLE_MINUTES = 5
APPROVED_BREADTH_DENOMINATOR = "advances_plus_declines"
ALTERNATE_BREADTH_DENOMINATOR = "all_classified_including_unchanged"

SECTOR_NAMES = (
    "Realty",
    "FMCG",
    "PSU Bank",
    "Financial Services",
    "Pharma",
    "Bank",
    "Auto",
    "IT",
    "Metal",
    "Energy",
    "Media",
    "Mid Select",
    "India Defence",
)

# Search keys only. A sector is not usable until the instrument master
# contains that exact trading symbol and the user approves it in Settings.
SECTOR_INDEX_SEARCH_KEYS = {
    "Realty": ("NIFTY REALTY", "NIFTYREALTY"),
    "FMCG": ("NIFTY FMCG", "NIFTYFMCG"),
    "PSU Bank": ("NIFTY PSU BANK", "NIFTYPSUBANK"),
    "Financial Services": ("FINNIFTY", "NIFTY FIN SERVICE", "NIFTYFINSERVICE"),
    "Pharma": ("NIFTY PHARMA", "NIFTYPHARMA"),
    "Bank": ("NIFTY BANK", "NIFTYBANK", "BANKNIFTY"),
    "Auto": ("NIFTY AUTO", "NIFTYAUTO"),
    "IT": ("NIFTY IT", "NIFTYIT"),
    "Metal": ("NIFTY METAL", "NIFTYMETAL"),
    "Energy": ("NIFTY ENERGY", "NIFTYENERGY"),
    "Media": ("NIFTY MEDIA", "NIFTYMEDIA"),
    "Mid Select": ("NIFTY MID SELECT", "NIFTYMIDSELECT"),
    "India Defence": ("NIFTY INDIA DEFENCE", "NIFTYINDIADEFENCE"),
}


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def candle_color(c: Candle) -> str:
    if c.close > c.open:
        return "GREEN"
    if c.close < c.open:
        return "RED"
    return "DOJI"


def pct_move(start: float, end: float) -> float:
    """Absolute percentage move. The boundary checks use this value with <=."""
    return abs(end - start) / start * 100 if start else 0.0


def rounded_price(value: float) -> float:
    return round(float(value), 2)


def classify_price(ltp: float, previous_close: float) -> str:
    """Compare prices after rounding to paise. Unchanged is its own state."""
    last = rounded_price(ltp)
    previous = rounded_price(previous_close)
    if last > previous:
        return "ADVANCE"
    if last < previous:
        return "DECLINE"
    return "UNCHANGED"


def change_pct(ltp: float, previous_close: float) -> Optional[float]:
    """Signed percentage from the same rounded prices used for breadth."""
    last = rounded_price(ltp)
    previous = rounded_price(previous_close)
    if previous == 0:
        return None
    return (last - previous) / previous * 100


def _evidence(label: str, actual, required: str, result: str) -> dict:
    return {"label": label, "actual": actual, "required": required, "result": result}


def _session_candles(candles: List[Candle]) -> List[Candle]:
    """Keep one trading date. A candle from another date is not an earlier bar."""
    if not candles:
        return []
    day = max(candle.ts for candle in candles).date()
    return sorted((candle for candle in candles if candle.ts.date() == day), key=lambda candle: candle.ts)


def _missing_earlier_bars(candles: List[Candle], index: int, minutes: int = DEFAULT_CANDLE_MINUTES) -> bool:
    window = candles[:index + 1]
    return any(
        right.ts - left.ts != timedelta(minutes=minutes)
        for left, right in zip(window, window[1:])
    )


def _volume_value(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def explain_setups(
    candles: List[Candle],
    direction: str,
    pdh: Optional[float],
    pdl: Optional[float],
    setup3_cutoff: time = DEFAULT_SETUP3_END,
    max_combined_pct: float = DEFAULT_COMBINED_MOVE,
    max_second_pct: float = DEFAULT_SECOND_MOVE,
    include_cutoff_candle: bool = False,
) -> dict:
    """Evaluate the three setups and keep every comparison, including failures.

    Setup 1 uses the current open and close, not the current high and low.
    Setup 3 requires volume below every earlier completed candle of the same day.
    """
    checks: List[dict] = []
    alerts: List[dict] = []
    candles = _session_candles(candles)
    if direction not in ("LONG", "SHORT"):
        return {"alerts": [], "checks": [_evidence("Direction", direction, "LONG or SHORT", "BLOCKED")]}
    if len(candles) < 2:
        checks.append(_evidence(
            "Completed 5-minute candles",
            f"{len(candles)} completed candle",
            "At least the first two completed candles",
            "BLOCKED",
        ))
        return {"alerts": [], "checks": checks}

    required_color = "RED" if direction == "LONG" else "GREEN"
    for index in range(1, min(len(candles), 3)):
        prev, cur = candles[index - 1], candles[index]
        color = candle_color(cur)
        open_inside = prev.low <= cur.open <= prev.high
        close_inside = prev.low <= cur.close <= prev.high
        color_ok = color == required_color
        candle_checks = [
            _evidence(
                f"Setup 1 {cur.ts.strftime('%H:%M')} candle colour",
                f"{color}; previous high {prev.high}; previous low {prev.low}",
                required_color,
                "PASS" if color_ok else "FAIL",
            ),
            _evidence(
                f"Setup 1 {cur.ts.strftime('%H:%M')} open inside previous range",
                cur.open, f"{prev.low} <= open <= {prev.high}", "PASS" if open_inside else "FAIL",
            ),
            _evidence(
                f"Setup 1 {cur.ts.strftime('%H:%M')} close inside previous range",
                cur.close, f"{prev.low} <= close <= {prev.high}", "PASS" if close_inside else "FAIL",
            ),
        ]
        checks.extend(candle_checks)
        if color_ok and open_inside and close_inside:
            alerts.append({
                "setup": "SETUP 1",
                "time": cur.ts.strftime("%H:%M"),
                "candle_time": cur.ts.strftime("%Y-%m-%d %H:%M:%S"),
                "detail": "Opposite-colour candle; open and close inside the previous candle high-low. High and low are not required to stay inside.",
                "previous_high": prev.high,
                "previous_low": prev.low,
                "trigger_close": cur.close,
                "open": cur.open,
                "high": cur.high,
                "low": cur.low,
                "close": cur.close,
                "volume": cur.volume,
                "color": color,
                "open_inside": True,
                "close_inside": True,
                "checks": candle_checks,
            })

    first, second = candles[0], candles[1]
    combined = pct_move(first.open, second.close)
    second_move = pct_move(second.open, second.close)
    if direction == "LONG":
        level_name = "Previous-day high"
        level = pdh
        crossed = level is not None and first.high > float(level)
        required = "First candle high strictly above previous-day high"
    else:
        level_name = "Previous-day low"
        level = pdl
        crossed = level is not None and first.low < float(level)
        required = "First candle low strictly below previous-day low"
    level_state = "BLOCKED" if level is None else ("PASS" if crossed else "FAIL")
    combined_state = "PASS" if combined <= max_combined_pct else "FAIL"
    second_state = "PASS" if second_move <= max_second_pct else "FAIL"
    setup2_checks = [
        _evidence(
            f"Setup 2 {level_name}",
            "unavailable" if level is None else f"level {level}; first high {first.high}; first low {first.low}; first open {first.open}",
            required,
            level_state,
        ),
        _evidence(
            "Setup 2 combined percentage",
            f"{round(combined, 4)} from open {first.open} to close {second.close}",
            f"<= {max_combined_pct}%",
            combined_state,
        ),
        _evidence(
            "Setup 2 second candle percentage",
            f"{round(second_move, 4)} from open {second.open} to close {second.close}",
            f"<= {max_second_pct}%",
            second_state,
        ),
    ]
    checks.extend(setup2_checks)
    if level is not None and crossed and combined_state == "PASS" and second_state == "PASS":
        alerts.append({
            "setup": "SETUP 2",
            "time": second.ts.strftime("%H:%M"),
            "candle_time": second.ts.strftime("%Y-%m-%d %H:%M:%S"),
            "detail": (
                f"First two candles; absolute move from candle 1 open to candle 2 close "
                f"{combined:.2f}%; candle 2 absolute move {second_move:.2f}%"
            ),
            "trigger_close": second.close,
            "open": second.open,
            "high": second.high,
            "low": second.low,
            "close": second.close,
            "volume": second.volume,
            "combined_move_pct": combined,
            "second_move_pct": second_move,
            "previous_day_high": pdh,
            "previous_day_low": pdl,
            "first_open": first.open,
            "first_high": first.high,
            "first_low": first.low,
            "checks": setup2_checks,
        })

    setup3_blocked = False
    for index in range(3, len(candles)):
        cur = candles[index]
        after_cutoff = cur.ts.time() > setup3_cutoff if include_cutoff_candle else cur.ts.time() >= setup3_cutoff
        stamp = cur.ts.strftime("%H:%M")
        if after_cutoff:
            checks.append(_evidence(
                f"Setup 3 {stamp} time eligibility",
                stamp,
                "From the fourth candle through the 10:55 candle. 11:00 and later are excluded.",
                "FAIL",
            ))
            continue
        if setup3_blocked or _missing_earlier_bars(candles, index):
            setup3_blocked = True
            checks.append(_evidence(
                f"Setup 3 {stamp} earlier candles",
                "A completed 5-minute candle is missing before this bar",
                "Every earlier completed candle of this session",
                "BLOCKED",
            ))
            continue
        prior = candles[:index]
        volumes = [_volume_value(candle.volume) for candle in prior]
        current_volume = _volume_value(cur.volume)
        if current_volume is None or any(volume is None for volume in volumes):
            checks.append(_evidence(
                f"Setup 3 {stamp} volume",
                "unavailable",
                "Current volume and every earlier completed volume",
                "BLOCKED",
            ))
            continue
        lowest = min(volumes)
        lower = current_volume < lowest
        color = candle_color(cur)
        color_ok = color == required_color
        candle_checks = [
            _evidence(
                f"Setup 3 {stamp} lower than every earlier candle",
                f"current {current_volume}; earlier {', '.join(str(volume) for volume in volumes)}; lowest {lowest}",
                f"current volume < {lowest}",
                "PASS" if lower else "FAIL",
            ),
            _evidence(f"Setup 3 {stamp} candle colour", color, required_color, "PASS" if color_ok else "FAIL"),
            _evidence(f"Setup 3 {stamp} time eligibility", stamp, "Fourth candle through 10:55", "PASS"),
        ]
        checks.extend(candle_checks)
        if lower and color_ok:
            alerts.append({
                "setup": "SETUP 3",
                "time": stamp,
                "candle_time": cur.ts.strftime("%Y-%m-%d %H:%M:%S"),
                "detail": f"Opposite-colour candle with volume {current_volume:g} below every earlier candle (lowest {lowest:g})",
                "trigger_close": cur.close,
                "open": cur.open,
                "high": cur.high,
                "low": cur.low,
                "close": cur.close,
                "volume": current_volume,
                "color": color,
                "earlier_volumes": volumes,
                "lowest_earlier_volume": lowest,
                "checks": candle_checks,
            })

    unique, seen = [], set()
    for item in alerts:
        key = (item["setup"], item["time"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return {"alerts": unique, "checks": checks}


def scan_setups(
    candles: List[Candle],
    direction: str,
    pdh: Optional[float],
    pdl: Optional[float],
    setup3_cutoff: time = DEFAULT_SETUP3_END,
    max_combined_pct: float = DEFAULT_COMBINED_MOVE,
    max_second_pct: float = DEFAULT_SECOND_MOVE,
    include_cutoff_candle: bool = False,
):
    """Return only the setups that passed. Explanations stay in explain_setups."""
    return explain_setups(
        candles, direction, pdh, pdl, setup3_cutoff, max_combined_pct, max_second_pct, include_cutoff_candle,
    )["alerts"]


def _group_stats(advances: int, declines: int, unchanged: int, denominator: str,
                 advance_min: float, decline_max: float):
    advances = int(advances)
    declines = int(declines)
    unchanged = int(unchanged)
    if denominator == ALTERNATE_BREADTH_DENOMINATOR:
        total = advances + declines + unchanged
    else:
        total = advances + declines
    if total <= 0:
        return {
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "total": total,
            "adv_pct": 0.0,
            "dec_pct": 0.0,
            "majority": "NONE",
            "qualified": False,
            "denominator": denominator,
        }
    adv_pct = advances / total * 100
    dec_pct = declines / total * 100
    majority = "BULLISH" if advances > declines else "BEARISH" if declines > advances else "TIE"
    qualified = (adv_pct >= advance_min and dec_pct <= decline_max) or (
        dec_pct >= advance_min and adv_pct <= decline_max
    )
    return {
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "total": total,
        "adv_pct": adv_pct,
        "dec_pct": dec_pct,
        "majority": majority,
        "qualified": qualified,
        "denominator": denominator,
    }


def classify_market(
    nifty_advances: int,
    nifty_declines: int,
    fno_advances: int,
    fno_declines: int,
    nifty_unchanged: int = 0,
    fno_unchanged: int = 0,
    advance_min: float = DEFAULT_ADVANCE_MIN,
    decline_max: float = DEFAULT_DECLINE_MAX,
    denominator: str = APPROVED_BREADTH_DENOMINATOR,
):
    """Classify breadth. The default denominator is advances + declines.

    Unchanged stocks are reported separately and are not treated as advances
    or declines. Including unchanged in the denominator is a different rule
    and stays off unless Settings records an explicit approval.
    """
    if min(nifty_advances, nifty_declines, fno_advances, fno_declines, nifty_unchanged, fno_unchanged) < 0:
        empty = _group_stats(0, 0, 0, denominator, advance_min, decline_max)
        return {
            "status": "NEUTRAL",
            "direction": None,
            "reason": "Breadth counts cannot be negative. Stock selection is off.",
            "nifty50": empty,
            "fno": empty,
        }

    n = _group_stats(nifty_advances, nifty_declines, nifty_unchanged, denominator, advance_min, decline_max)
    f = _group_stats(fno_advances, fno_declines, fno_unchanged, denominator, advance_min, decline_max)
    if n["total"] == 0 or f["total"] == 0:
        status, direction, reason = "NEUTRAL", None, "Enter valid breadth data for both market groups."
    elif n["majority"] in ("BULLISH", "BEARISH") and f["majority"] in ("BULLISH", "BEARISH") and n["majority"] != f["majority"]:
        status, direction, reason = (
            "OPPOSITE",
            None,
            "Nifty 50 and Securities in F&O are moving in opposite directions. Stock selection is off.",
        )
    elif n["majority"] == "TIE" or f["majority"] == "TIE":
        status, direction, reason = (
            "NEUTRAL",
            None,
            "Advances and declines are equal in at least one group. Stock selection is off.",
        )
    elif not n["qualified"] or not f["qualified"]:
        status, direction, reason = (
            "NEUTRAL",
            None,
            f"Both groups must meet the {advance_min:g}:{decline_max:g} breadth threshold. Stock selection is off.",
        )
    elif n["majority"] == "BULLISH" and f["majority"] == "BULLISH":
        status, direction, reason = (
            "BULLISH",
            "LONG",
            f"Both groups have advances of at least {advance_min:g}% and declines of at most {decline_max:g}%. Long stock selection is allowed.",
        )
    elif n["majority"] == "BEARISH" and f["majority"] == "BEARISH":
        status, direction, reason = (
            "BEARISH",
            "SHORT",
            f"Both groups have declines of at least {advance_min:g}% and advances of at most {decline_max:g}%. Short stock selection is allowed.",
        )
    else:
        status, direction, reason = "NEUTRAL", None, "The market filter is not met. Stock selection is off."
    return {"status": status, "direction": direction, "reason": reason, "nifty50": n, "fno": f}


def eligible(
    change_pct: float,
    direction: str,
    advances: int = 0,
    declines: int = 0,
    market_status: str = "",
    max_abs_move: float = DEFAULT_MAX_ABS_MOVE,
):
    """Candidate filter. Callers pass the verified market gate status."""
    del advances, declines
    if market_status and market_status not in ("BULLISH", "BEARISH"):
        return False
    if abs(change_pct) > max_abs_move:
        return False
    if direction == "LONG":
        return (not market_status or market_status == "BULLISH") and change_pct > 0
    if direction == "SHORT":
        return (not market_status or market_status == "BEARISH") and change_pct < 0
    return False


def assess_quote_group(quotes_by_symbol: dict, required_symbols: Sequence[str], now: datetime, max_age_seconds: float):
    """Fail closed unless every required symbol has a fresh price and previous close."""
    missing, stale = [], []
    counts = {"ADVANCE": 0, "DECLINE": 0, "UNCHANGED": 0}
    priced = []
    if not required_symbols:
        return {"complete": False, "missing": [], "stale": [], "counts": None, "priced": [], "reason": "The symbol list is empty."}
    for symbol in required_symbols:
        quote = quotes_by_symbol.get(symbol)
        if not quote or quote.get("ltp") is None or quote.get("prev_close") is None:
            missing.append(symbol)
            continue
        as_of = quote.get("as_of")
        if not isinstance(as_of, datetime) or (now - as_of).total_seconds() > max_age_seconds:
            stale.append(symbol)
            continue
        state = classify_price(quote["ltp"], quote["prev_close"])
        counts[state] += 1
        pct = change_pct(quote["ltp"], quote["prev_close"])
        priced.append({
            "symbol": symbol,
            "ltp": quote["ltp"],
            "prev_close": quote["prev_close"],
            "change_pct": pct,
            "state": state,
            "name": quote.get("name") or symbol,
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
        })
    complete = not missing and not stale
    reason = "Complete fresh breadth."
    if missing:
        reason = f"{len(missing)} symbols have no valid price or previous close."
    elif stale:
        reason = f"{len(stale)} symbols have stale prices."
    return {
        "complete": complete,
        "missing": missing,
        "stale": stale,
        "counts": counts if complete else None,
        "priced": priced if complete else [],
        "reason": reason,
    }


def market_from_groups(nifty_group: dict, fno_group: dict, **thresholds):
    if not nifty_group.get("complete") or not fno_group.get("complete"):
        blank = _group_stats(0, 0, 0, thresholds.get("denominator", APPROVED_BREADTH_DENOMINATOR),
                             thresholds.get("advance_min", DEFAULT_ADVANCE_MIN),
                             thresholds.get("decline_max", DEFAULT_DECLINE_MAX))
        reason = "Market breadth was not calculated because price data is incomplete or stale."
        details = []
        if not nifty_group.get("complete"):
            details.append(f"Nifty 50: {nifty_group.get('reason', 'incomplete')}")
        if not fno_group.get("complete"):
            details.append(f"F&O: {fno_group.get('reason', 'incomplete')}")
        return {
            "status": "NEUTRAL",
            "direction": None,
            "reason": reason + " " + " ".join(details),
            "nifty50": blank,
            "fno": blank,
            "complete": False,
        }
    n, f = nifty_group["counts"], fno_group["counts"]
    result = classify_market(
        n["ADVANCE"], n["DECLINE"], f["ADVANCE"], f["DECLINE"],
        nifty_unchanged=n["UNCHANGED"], fno_unchanged=f["UNCHANGED"],
        **thresholds,
    )
    result["complete"] = True
    return result


def rank_rows(rows: Sequence[dict], direction: str, limit: int, tie_break_approved: bool, value_key: str = "change_pct"):
    """Rank eligible rows. A tie that crosses the cutoff is refused until approved."""
    if direction not in ("LONG", "SHORT"):
        return [], "Direction is not LONG or SHORT."
    usable = [row for row in rows if row.get(value_key) is not None and row.get("symbol")]
    reverse = direction == "LONG"
    ordered = sorted(usable, key=lambda row: row[value_key], reverse=reverse)
    if len(ordered) > limit:
        boundary = ordered[limit - 1][value_key]
        if any(row[value_key] == boundary for row in ordered[limit:]) and not tie_break_approved:
            return [], (
                "The ranking cutoff has a tie. Approve the symbol A-Z tie-break in Settings "
                "before this method can drop one of the tied stocks."
            )
        if tie_break_approved:
            ordered = sorted(usable, key=lambda row: ((-row[value_key] if reverse else row[value_key]), row["symbol"]))
    return ordered[:limit], None


def select_price_leaders(rows: Sequence[dict], direction: str, market_status: str, limit: int = DEFAULT_TOP_N,
                         max_abs_move: float = DEFAULT_MAX_ABS_MOVE, tie_break_approved: bool = False):
    kept = [row for row in rows if row.get("change_pct") is not None and eligible(
        row["change_pct"], direction, market_status=market_status, max_abs_move=max_abs_move
    )]
    return rank_rows(kept, direction, limit, tie_break_approved)


def select_sectors(sector_moves: Sequence[dict], direction: str, count: int = DEFAULT_SECTOR_COUNT,
                   tie_break_approved: bool = False, required_names: Sequence[str] = SECTOR_NAMES):
    """Rank sectors only when every required sector has one fresh move."""
    by_name = {row["sector"]: row for row in sector_moves if row.get("sector") and row.get("move_pct") is not None}
    missing = [name for name in required_names if name not in by_name]
    if missing:
        return [], f"Sector ranking needs a verified move for every sector. Missing: {', '.join(missing)}."
    ranked, error = rank_rows(
        [{"symbol": name, "change_pct": by_name[name]["move_pct"], **by_name[name]} for name in required_names],
        direction,
        count,
        tie_break_approved,
    )
    return ranked, error


def select_sector_stocks(stocks: Sequence[dict], direction: str, market_status: str, limit: int = DEFAULT_STOCKS_PER_SECTOR,
                         max_abs_move: float = DEFAULT_MAX_ABS_MOVE, tie_break_approved: bool = False):
    return select_price_leaders(stocks, direction, market_status, limit, max_abs_move, tie_break_approved)


def merge_candidates(picked_by_source: dict) -> list:
    merged = {}
    for source, rows in picked_by_source.items():
        for row in rows:
            symbol = row["symbol"]
            current = merged.get(symbol)
            if current is None:
                current = dict(row)
                current["sources"] = []
                current["ranks"] = []
                merged[symbol] = current
            if source not in current["sources"]:
                current["sources"].append(source)
            label = row.get("rank_label")
            if label and label not in current["ranks"]:
                current["ranks"].append(label)
            if row.get("sector") and not current.get("sector"):
                current["sector"] = row.get("sector")
    return [merged[symbol] for symbol in sorted(merged)]


def signal_document_id(trading_date: str, symbol: str, setup: str, candle_time: str, direction: str) -> str:
    raw = f"{trading_date}_{symbol}_{setup}_{direction}_{candle_time}"
    return "".join(ch if ch.isalnum() or ch in ("_", "-") else "" for ch in raw.replace(":", "").replace(" ", "_"))


def snapshot_is_lockable(snapshot_time: datetime, now: datetime, deadline: time = DEFAULT_SELECTION_DEADLINE,
                         max_age_seconds: float = 30) -> bool:
    """Lock only a snapshot taken at or before the deadline and still fresh."""
    snap_t = snapshot_time.time().replace(microsecond=0)
    dead_t = deadline.replace(microsecond=0)
    if snap_t > dead_t:
        return False
    age = (now.replace(microsecond=0) - snapshot_time.replace(microsecond=0)).total_seconds()
    return 0 <= age <= max_age_seconds


def completed_candle(candle: Candle, now: datetime, interval_minutes: int = DEFAULT_CANDLE_MINUTES) -> bool:
    return candle.ts + timedelta(minutes=interval_minutes) <= now


def session_slots(trading_date, now: datetime, open_time: time = DEFAULT_OPEN, close_time: time = DEFAULT_CLOSE,
                  interval_minutes: int = DEFAULT_CANDLE_MINUTES) -> list:
    start = datetime.combine(trading_date, open_time)
    slots = []
    cursor = start
    while cursor + timedelta(minutes=interval_minutes) <= now and cursor.time() < close_time:
        slots.append(cursor)
        cursor += timedelta(minutes=interval_minutes)
    return slots


def missing_session_slots(candles: Iterable[Candle], trading_date, now: datetime, **kwargs) -> list:
    have = {candle.ts.replace(second=0, microsecond=0) for candle in candles}
    return [slot for slot in session_slots(trading_date, now, **kwargs) if slot not in have]


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def parse_candle_time(value) -> datetime:
    """Treat Groww candle timestamps as Asia/Kolkata wall time."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(IST).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp = stamp / 1000.0
        return datetime.fromtimestamp(stamp, IST).replace(tzinfo=None)
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised candle time: {value}")
