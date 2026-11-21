"""Turn a locked candidate list into the client-facing decision table.

This module does not fetch prices. Missing optional fields stay missing.
"""

from __future__ import annotations

from scanner_rules import eligible


METHOD_LABELS = {
    "Method 1": "Method 1 — top F&O price movers",
    "Method 2": "Method 2 — top two sectors",
    "Top movers": "Method 1 — top F&O price movers",
    "Sector movers": "Method 2 — top two sectors",
}

COVERAGE = (
    ("Nifty 50 breadth", "tested", "Advances, declines, unchanged and the percentage are calculated for the Nifty 50 list."),
    ("F&O breadth", "tested", "The same breadth calculation is applied to NSE F&O equities."),
    ("60:40 market-direction rule", "tested", "Both groups must agree. Neutral and opposite mean no trade."),
    ("Top five F&O gainers or losers", "tested", "Method 1 ranks daily percentage change. This is the written price-mover rule."),
    ("All 13 sector rankings", "sectors", "Each sector needs one verified Groww index price. Energy and India Defence are absent from the current instrument file."),
    ("Top two sector selection", "sectors", "The top two are chosen only after all 13 sectors have a comparable index move."),
    ("Top five stocks per selected sector", "sectors", "Official constituents are intersected with the F&O universe, then filtered by direction and 5%."),
    ("NSE-wide top five gainers or losers", "nse", "Groww's documented SDK has no NSE-wide gainer or loser ranking."),
    ("F&O-only filter", "tested", "A selected stock must be an NSE F&O underlying with an NSE cash series EQ listing."),
    ("Maximum 5% movement rule", "tested", "A move above 5% is excluded. Exactly 5% stays eligible."),
    ("9:30 AM candidate lock", "tested", "The list is locked from the last fresh snapshot at or before 9:30. Later prices do not rebuild it."),
    ("Setup 1", "tested", "The second or third completed candle must be the opposite colour, and its open and close must both sit inside the previous candle high-low. Its own high and low are not required to stay inside."),
    ("Setup 2", "tested", "The first candle must break the previous day high or low. The combined move is at most 2.5% and the second candle move is at most 1%, both inclusive."),
    ("Setup 3", "tested", "From the fourth candle through the 10:55 candle. Volume must be strictly lower than every earlier completed candle. The 11:00 candle is excluded."),
    ("Completed 5-minute candle validation", "tested", "A candle is used only after its five minutes have elapsed."),
    ("Duplicate-signal prevention", "tested", "The same date, symbol, setup, candle time and direction is stored once."),
    ("Post-lock reversal behaviour", "reversal", "The locked list is not rebuilt. Whether a later breadth change should stop new signals is not confirmed."),
    ("Final BUY, SHORT and WAIT decision", "tested", "A setup is required before BUY or SHORT. A selected stock without a setup stays WAIT."),
)


def _mark(state: str) -> str:
    return {
        "PASS": "✓ PASS",
        "FAIL": "✗ FAIL",
        "NOT APPLICABLE": "— NOT APPLICABLE",
        "BLOCKED": "! BLOCKED",
        "NOT DEFINED": "? NOT DEFINED",
    }[state]


def _check(state: str, label: str, actual: str, required: str) -> dict:
    return {
        "state": state,
        "mark": _mark(state),
        "label": label,
        "actual": actual,
        "required": required,
    }


def _na(value):
    if value is None or value == "":
        return "N/A"
    return value


def _num(value, digits: int = 2):
    if value is None or value == "":
        return "N/A"
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return "N/A"


def strategy_coverage(sector_ready: bool) -> list[dict]:
    rows = []
    for name, kind, detail in COVERAGE:
        if kind == "nse":
            status = "BLOCKED — DATA SOURCE REQUIRED"
        elif kind == "sectors" and not sector_ready:
            status = "BLOCKED — DATA SOURCE REQUIRED"
        elif kind == "reversal":
            status = "NEEDS CLIENT CLARIFICATION"
        else:
            status = "IMPLEMENTED AND TESTED"
        rows.append({"requirement": name, "status": status, "detail": detail})
    rows.append({
        "requirement": "Genuine OI-spurt definition",
        "status": "NEEDS CLIENT CLARIFICATION",
        "detail": "Price-mover selection implemented. Separate OI-spurt definition requires client clarification.",
    })
    rows.append({
        "requirement": "Live Groww prices, candles and open interest",
        "status": "IMPLEMENTED BUT LIVE DATA NOT VERIFIED",
        "detail": "The calls exist. A successful login does not prove these permissions. Run Test Groww permissions.",
    })
    return rows


def coverage_is_complete(rows: list[dict]) -> bool:
    return all(row["status"] == "IMPLEMENTED AND TESTED" for row in rows)


def _signals_for(symbol: str, signals: list[dict]) -> list[dict]:
    return [row for row in signals or [] if row.get("symbol") == symbol and row.get("origin", "live") == "live"]


def _decision(market_status: str, direction: str | None, selected: bool, setups: list[dict], blocked: bool) -> tuple[str, str]:
    if blocked:
        return "BLOCKED", "Required market data is missing or stale, so this stock is not a recommendation."
    if market_status in ("NEUTRAL", "OPPOSITE") or not direction:
        return "NO TRADE", "The market gate did not confirm one direction, so no buy or short is allowed."
    if not selected:
        return "EXCLUDED", "The stock did not pass the common selection filters."
    if not setups:
        return "WAIT", "The stock is selected and is waiting for a completed setup. It is not a buy or a short yet."
    if direction == "LONG":
        return "BUY", "The locked market is bullish and at least one completed setup passed."
    if direction == "SHORT":
        return "SHORT", "The locked market is bearish and at least one completed setup passed."
    return "NO TRADE", "No trade direction is available."


def _checklist(market: dict, row: dict, selected: bool, setups: list[dict], fresh: bool, duplicate: bool) -> list[dict]:
    nifty = (market or {}).get("nifty50") or {}
    fno = (market or {}).get("fno") or {}
    status = (market or {}).get("status") or ""
    direction = (market or {}).get("direction")
    agreed = status in ("BULLISH", "BEARISH") and bool(direction)
    change = row.get("change_pct")
    in_direction = False
    if direction and change is not None:
        in_direction = eligible(float(change), direction, market_status=status or "BULLISH")
    within_five = change is not None and abs(float(change)) <= 5
    sector_state = "NOT APPLICABLE"
    sector_actual = "Not selected through a sector."
    if "Method 2" in (row.get("sources") or []) or "Sector movers" in (row.get("sources") or []):
        sector_state = "PASS" if row.get("sector") else "FAIL"
        sector_actual = row.get("sector") or "Sector name missing"
    setup_state = "PASS" if setups else ("FAIL" if selected else "NOT APPLICABLE")
    stored_checks = list(row.get("setup_checks") or [])
    for item in setups:
        stored_checks.extend(item.get("checks") or [])
    if any(item.get("result") == "BLOCKED" and item.get("label") == "Completed 5-minute candles" for item in stored_checks):
        candle_state = "BLOCKED"
    elif stored_checks or setups:
        candle_state = "PASS"
    else:
        candle_state = "NOT APPLICABLE"
    rows = [
        _check("PASS" if nifty.get("qualified") else "FAIL", "Nifty 50 market breadth confirmed", f"{nifty.get('adv_pct', 'n/a')}% advances", "At least 60% advances and at most 40% declines, or the bearish reverse"),
        _check("PASS" if fno.get("qualified") else "FAIL", "F&O market breadth confirmed", f"{fno.get('adv_pct', 'n/a')}% advances", "At least 60% advances and at most 40% declines, or the bearish reverse"),
        _check("PASS" if agreed else "FAIL", "Both market groups agree", status or "unavailable", "BULLISH or BEARISH"),
        _check("PASS" if row.get("in_fno", True) else "FAIL", "Stock belongs to the NSE F&O universe", row.get("symbol") or "", "NSE F&O underlying with NSE cash series EQ"),
        _check("PASS" if in_direction else "FAIL", "Stock moves in the confirmed direction", _na(change), "Positive for bullish, negative for bearish, and not above 5%"),
        _check("PASS" if within_five else "FAIL", "Daily movement is within 5%", _na(change), "Absolute move of 5% or less"),
        _check("PASS" if selected else "FAIL", "Qualifies through at least one selection method", ", ".join(row.get("sources") or []) or "none", "Method 1 or Method 2"),
        _check(sector_state, "Sector and ranking conditions", sector_actual, "Required only for a Method 2 selection"),
        _check(candle_state, "Required 5-minute candle is completed", "Evaluated" if candle_state == "PASS" else "Not evaluated", "A forming candle is ignored"),
        _check(setup_state, "At least one trading setup passes", ", ".join(item.get("setup", "") for item in setups) or "none yet", "Setup 1, Setup 2 or Setup 3 on a completed candle"),
        _check("PASS" if fresh else "FAIL", "Market data is fresh and valid", row.get("data_updated") or "unavailable", "Price age inside the freshness window"),
        _check("PASS" if not duplicate else "FAIL", "Signal is not a duplicate", "First time for this candle" if not duplicate else "Already stored", "One alert per date, symbol, setup, candle and direction"),
        _check("NOT DEFINED", "OI-spurt rule", _na(row.get("oi_change")), "No client OI-spurt definition has been supplied"),
    ]
    seen = set()
    for item in stored_checks:
        label = item.get("label")
        if not label or label in seen:
            continue
        seen.add(label)
        result = item.get("result") or "BLOCKED"
        if result not in ("PASS", "FAIL", "BLOCKED", "NOT DEFINED", "NOT APPLICABLE"):
            result = "BLOCKED"
        rows.append(_check(result, label, item.get("actual"), item.get("required") or ""))
    return rows


def build_board(market: dict | None, candidates: list[dict] | None, considered: list[dict] | None,
                signals: list[dict] | None, methods: dict | None, trading_date: str,
                data_updated: str = "", sector_ready: bool = False, locked: bool = False) -> dict:
    market = market or {}
    methods = methods or {}
    coverage = strategy_coverage(sector_ready)
    blocked = not market.get("complete", False)
    status = market.get("status") or ""
    direction = market.get("direction")
    if not locked:
        return {
            "banner": "WAITING FOR 9:30",
            "reason": "The candidate list is locked at 9:30 IST from the last fresh snapshot. No list is invented before that.",
            "rows": [],
            "coverage": coverage,
            "coverage_complete": False,
            "methods": methods,
            "summary": {"buy": [], "short": [], "wait": [], "excluded": 0, "blocked": 0},
        }
    if blocked:
        return {
            "banner": "BLOCKED",
            "reason": market.get("reason") or "Market data is incomplete or stale. No trade is recommended.",
            "rows": [],
            "coverage": coverage,
            "coverage_complete": False,
            "methods": methods,
            "summary": {"buy": [], "short": [], "wait": [], "excluded": 0, "blocked": 1},
        }
    if status in ("NEUTRAL", "OPPOSITE") or not direction:
        return {
            "banner": "NO TRADE",
            "reason": market.get("reason") or "The two market groups do not confirm one direction.",
            "rows": [],
            "coverage": coverage,
            "coverage_complete": coverage_is_complete(coverage),
            "methods": methods,
            "summary": {"buy": [], "short": [], "wait": [], "excluded": 0, "blocked": 0},
        }

    selected_symbols = {row.get("symbol") for row in candidates or []}
    pool = list(candidates or [])
    for row in considered or []:
        if row.get("symbol") not in selected_symbols:
            pool.append(row)
    table = []
    for row in pool:
        symbol = row.get("symbol")
        selected = symbol in selected_symbols
        setups = _signals_for(symbol, signals or [])
        fresh = row.get("current_price_status", "live") == "live" and row.get("ltp") not in (None, "")
        decision, reason = _decision(status, direction, selected, setups, False)
        if not fresh and selected and not setups:
            decision, reason = "BLOCKED", "The latest price is missing or stale, so a new decision is not shown as a trade."
        change = row.get("change_pct")
        previous_close = row.get("prev_close")
        ltp = row.get("ltp") if fresh else row.get("ltp")
        rupee = None
        if ltp not in (None, "") and previous_close not in (None, ""):
            rupee = round(float(ltp) - float(previous_close), 2)
        checks = _checklist(market, row, selected, setups, fresh, False)
        proof = [f"{item['mark']} {item['label']}. Actual: {item['actual']}. Required: {item['required']}." for item in checks]
        for setup in setups:
            proof.append(
                f"{setup.get('setup')} at {setup.get('candle_time')}. "
                f"OHLC {setup.get('open')}, {setup.get('high')}, {setup.get('low')}, {setup.get('close')}. "
                f"{setup.get('detail') or ''}"
            )
        sources = [METHOD_LABELS.get(source, source) for source in (row.get("sources") or [])]
        table.append({
            "Date": trading_date,
            "Symbol": symbol,
            "Company": row.get("name") or symbol,
            "Sector": row.get("sector") or "N/A",
            "LTP": _num(ltp),
            "Previous Close": _num(previous_close),
            "Change": _num(rupee),
            "Change %": _num(change),
            "Day High": _num(row.get("day_high")),
            "Day Low": _num(row.get("day_low")),
            "Previous Day High": _num(row.get("previous_day_high")),
            "Previous Day Low": _num(row.get("previous_day_low")),
            "Volume": _na(row.get("volume")),
            "OI": _na(row.get("open_interest")),
            "OI Change": _na(row.get("oi_change")),
            "Market Direction": status,
            "Selected By": ", ".join(sources) if sources else "None",
            "Selection Rank": ", ".join(row.get("ranks") or []) or "N/A",
            "Setup": ", ".join(item.get("setup", "") for item in setups) or "None yet",
            "Candle Time": ", ".join(item.get("candle_time", "") for item in setups) or "N/A",
            "Setup OHLC": "; ".join(
                f"{item.get('setup')} {item.get('open')}/{item.get('high')}/{item.get('low')}/{item.get('close')}"
                for item in setups
            ) or "N/A",
            "Strategy Checks": " ".join(item["mark"] + " " + item["label"] for item in checks),
            "Final Decision": decision,
            "Reason": reason,
            "Data Source": "Groww" if fresh else "N/A",
            "Data Updated": data_updated or "N/A",
            "checks": checks,
            "proof": proof,
        })
    order = {"BUY": 0, "SHORT": 1, "WAIT": 2, "BLOCKED": 3, "EXCLUDED": 4, "NO TRADE": 5}
    table.sort(key=lambda item: (order.get(item["Final Decision"], 9), item["Symbol"]))
    summary = {
        "buy": [row["Symbol"] for row in table if row["Final Decision"] == "BUY"],
        "short": [row["Symbol"] for row in table if row["Final Decision"] == "SHORT"],
        "wait": [row["Symbol"] for row in table if row["Final Decision"] == "WAIT"],
        "excluded": len([row for row in table if row["Final Decision"] == "EXCLUDED"]),
        "blocked": len([row for row in table if row["Final Decision"] == "BLOCKED"]),
    }
    return {
        "banner": status,
        "reason": market.get("reason") or "",
        "rows": table,
        "coverage": coverage,
        "coverage_complete": coverage_is_complete(coverage),
        "methods": methods,
        "summary": summary,
    }
