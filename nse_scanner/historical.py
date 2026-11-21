"""Historical rule checks. Results are always marked historical, never live."""

from __future__ import annotations

from datetime import datetime, timedelta

from nse_scanner.state import write_json
from scanner_rules import Candle, classify_market, completed_candle, eligible, parse_candle_time, scan_setups

SETUP3_DISCREPANCY = (
    "Setup 3 uses candles from the fourth completed bar through the bar that starts at 10:55. "
    "A candle that starts at 11:00 is excluded. That is the client's written cutoff."
)
REVERSAL_DIFFERENCE = (
    "NEEDS CLIENT CONFIRMATION. The 9:30 candidate list is not rebuilt from later prices. "
    "The current program still scans that locked list after the live breadth changes. "
    "Stopping new signals after a reversal is not turned on."
)


def parse_candle_text(raw: str) -> list[Candle]:
    rows = []
    for line in raw.strip().splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != 6:
            raise ValueError("Each candle row needs time, open, high, low, close, volume.")
        stamp = parts[0]
        if len(stamp) == 5:
            stamp = datetime.now().strftime("%Y-%m-%d ") + stamp
        rows.append(Candle(parse_candle_time(stamp), *[float(value) for value in parts[1:]]))
    rows.sort(key=lambda candle: candle.ts)
    return rows


def run_historical(nifty_advances: int, nifty_declines: int, fno_advances: int, fno_declines: int,
                   candles: list[Candle], direction_hint: str, pdh: float, pdl: float,
                   change_pct: float | None = None) -> dict:
    market = classify_market(nifty_advances, nifty_declines, fno_advances, fno_declines)
    direction = market["direction"] or direction_hint
    alerts = []
    blocked = market["status"] not in ("BULLISH", "BEARISH")
    move_blocked = change_pct is not None and not eligible(change_pct, direction or "NO TRADE", market_status=market["status"])
    if not blocked and not move_blocked and direction in ("LONG", "SHORT"):
        alerts = scan_setups(candles, direction, pdh, pdl)
    return {
        "origin": "historical",
        "live": False,
        "market": market,
        "alerts": alerts,
        "move_blocked": move_blocked,
        "note": "Historical test only. These rows are not live signals and no order is sent.",
    }


def _candle(day: str, clock: str, open_, high, low, close, volume) -> Candle:
    return Candle(datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M"), open_, high, low, close, volume)


def _names(alerts) -> list[str]:
    return [f"{item['setup']} {item['time']}" for item in alerts]


def build_validation_report(day: str = "2026-11-20") -> dict:
    """Replay fixed completed candles and compare each setup with the expected result.

    The report checks the rules only. It does not estimate profit or loss.
    """
    cases = []

    def check(name: str, actual, expected) -> None:
        cases.append({"name": name, "expected": expected, "actual": actual, "passed": actual == expected})

    opening = _candle(day, "09:15", 100, 102, 99, 101, 1000)
    inside = _candle(day, "09:20", 101, 101.5, 100, 100.4, 900)
    check("Setup 1 matches an inside opposite candle", _names(scan_setups([opening, inside], "LONG", 200, 50)), ["SETUP 1 09:20"])
    doji = _candle(day, "09:20", 101, 102, 100, 101, 800)
    check("Setup 1 rejects a doji", _names(scan_setups([opening, doji], "LONG", 200, 50)), [])

    setup2 = [
        _candle(day, "09:15", 100, 111, 99, 110, 1000),
        _candle(day, "09:20", 102, 103, 101, 102.5, 900),
    ]
    check("Setup 2 includes a 2.5 percent combined move and a 1 percent second candle", _names(scan_setups(setup2, "LONG", 110, 90)), ["SETUP 2 09:20"])
    too_far = [
        _candle(day, "09:15", 100, 111, 99, 110, 1000),
        _candle(day, "09:20", 102, 103, 101, 102.5001, 900),
    ]
    check("Setup 2 rejects a second candle above the 1 percent cap", _names(scan_setups(too_far, "LONG", 110, 90)), [])

    base = [
        _candle(day, "09:15", 100, 101, 99, 100.5, 100),
        _candle(day, "09:20", 100.5, 101, 100, 100.8, 90),
        _candle(day, "09:25", 100.8, 101, 100.2, 100.9, 80),
    ]
    lower = base + [_candle(day, "09:30", 101, 101.2, 100.4, 100.5, 70)]
    check("Setup 3 matches strictly lower volume", _names(scan_setups(lower, "LONG", 200, 50)), ["SETUP 3 09:30"])
    equal = base + [_candle(day, "09:30", 101, 101.2, 100.4, 100.5, 80)]
    check("Setup 3 rejects equal volume", _names(scan_setups(equal, "LONG", 200, 50)), [])
    def through(clock: str):
        start = datetime.strptime(f"{day} 09:15", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M")
        rows = []
        cursor = start
        index = 0
        while cursor <= end:
            rows.append(_candle(day, cursor.strftime("%H:%M"), 101, 102, 99, 100.5, 1 if cursor == end else 1000 - index))
            cursor += timedelta(minutes=5)
            index += 1
        return rows

    cutoff_names = [name for name in _names(scan_setups(through("10:55"), "LONG", 200, 50)) if name.startswith("SETUP 3")]
    check("Setup 3 includes the 10:55 candle", "SETUP 3 10:55" in cutoff_names, True)
    eleven_names = [name for name in _names(scan_setups(through("11:00"), "LONG", 200, 50)) if name.startswith("SETUP 3")]
    check("Setup 3 excludes a candle that starts at 11:00", "SETUP 3 11:00" not in eleven_names and "SETUP 3 10:55" in eleven_names, True)
    after_names = [name for name in _names(scan_setups(through("11:05"), "LONG", 200, 50)) if name.startswith("SETUP 3")]
    check("Setup 3 excludes the 11:05 candle", "SETUP 3 11:05" not in after_names, True)

    incomplete_now = datetime.strptime(f"{day} 09:19", "%Y-%m-%d %H:%M")
    complete_now = datetime.strptime(f"{day} 09:20", "%Y-%m-%d %H:%M")
    check("A candle is not complete before its five minutes have elapsed", completed_candle(opening, incomplete_now), False)
    check("A candle is complete once its five minutes have elapsed", completed_candle(opening, complete_now), True)

    from nse_scanner.candles import empty_cache, merge_candles, parse_groww_candles, store_previous_day
    from nse_scanner.engine import signals_for_candidate
    from nse_scanner.settings import DEFAULTS

    cache = empty_cache(day)
    merge_candles(cache, "RELIANCE", parse_groww_candles({"candles": [
        [f"{day}T09:15:00", 100, 102, 99, 101, 1000, None],
        [f"{day}T09:20:00", 101, 101.5, 100, 100.4, 900, None],
    ]}))
    store_previous_day(cache, "RELIANCE", [
        {"ts": "2026-11-19 00:00:00", "open": 90, "high": 110, "low": 80, "close": 100, "volume": 1}
    ], day)
    now = datetime.strptime(f"{day} 09:25", "%Y-%m-%d %H:%M")
    candidate = {"symbol": "RELIANCE", "name": "Reliance", "ltp": 101, "change_pct": 1, "sources": ["Top movers"]}
    first, _note = signals_for_candidate(cache, candidate, "LONG", now.date(), now, DEFAULTS, set())
    second, _note = signals_for_candidate(cache, candidate, "LONG", now.date(), now, DEFAULTS, {row["id"] for row in first})
    check("The first completed Setup 1 is reported once", [row["setup"] for row in first], ["SETUP 1"])
    check("The same signal is not reported again", second, [])

    passed = all(case["passed"] for case in cases)
    return {
        "origin": "historical",
        "live": False,
        "passed": passed,
        "case_count": len(cases),
        "failed_count": len([case for case in cases if not case["passed"]]),
        "cases": cases,
        "setup3_discrepancy": SETUP3_DISCREPANCY,
        "reversal_difference": REVERSAL_DIFFERENCE,
        "profitability": "Not calculated. No entry rule or exit rule is defined, so this report is not a trading result.",
        "note": "Historical rule validation only. These rows are not live signals and no order is sent.",
    }


def write_validation_report(day: str = "2026-11-20") -> dict:
    report = build_validation_report(day)
    write_json("historical_validation_report.json", report)
    return report
