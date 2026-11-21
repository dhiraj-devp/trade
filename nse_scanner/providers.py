"""External ranking sources.

Groww's documented SDK can price a known symbol. It cannot supply an NSE-wide
gainer list or a sector index that is absent from the instrument master.
Missing sources stay empty. Nothing is estimated.
"""

from __future__ import annotations

from scanner_rules import SECTOR_NAMES, eligible


NSE_RANKING_BLOCK = (
    "BLOCKED — DATA SOURCE REQUIRED. Groww's documented SDK has no NSE-wide gainer or loser ranking. "
    "An authorized ranking provider and its credential are required. "
    "F&O prices are not used as a substitute."
)

OI_STATUS = (
    "Price-mover selection implemented. Separate OI-spurt definition requires client clarification."
)

REVERSAL_STATUS = (
    "NEEDS CLIENT CONFIRMATION. The 9:30 list is not rebuilt from later prices. "
    "The current code still scans that locked list after the live breadth changes. "
    "Stopping new signals after a reversal has not been confirmed by the client."
)


def select_nse_intersection(nse_rows: list[dict] | None, fno_rows: list[dict], direction: str,
                            market_status: str, limit: int = 5, max_abs_move: float = 5.0) -> tuple[list[dict], str]:
    """Keep the provider's order. Drop names that are not eligible F&O stocks."""
    if not nse_rows:
        return [], NSE_RANKING_BLOCK
    fno = {row["symbol"]: row for row in fno_rows if row.get("symbol")}
    kept = []
    for row in nse_rows:
        symbol = row.get("symbol")
        local = fno.get(symbol)
        change = row.get("change_pct")
        if local is None or change is None:
            continue
        if not eligible(float(change), direction, market_status=market_status, max_abs_move=max_abs_move):
            continue
        copied = dict(local)
        copied["change_pct"] = float(change)
        copied["nse_rank"] = row.get("rank")
        copied["ranking_source"] = row.get("source") or "configured_provider"
        copied["ranking_universe"] = row.get("universe") or "NSE cash equities"
        copied["ranked_at"] = row.get("ranked_at")
        kept.append(copied)
        if len(kept) >= limit:
            break
    if not kept:
        return [], "The NSE ranking contained no F&O stock that passed the direction and 5% filters."
    return kept, "Method 3 used the supplied NSE-wide ranking, then kept only eligible F&O stocks."


def sector_index_gap(matches: dict[str, list]) -> list[str]:
    """Sectors with no single exact index symbol. Ambiguous matches are also a gap."""
    missing = []
    for name in SECTOR_NAMES:
        symbols = matches.get(name) or []
        unique = {item if isinstance(item, str) else item.get("trading_symbol") for item in symbols}
        unique.discard(None)
        unique.discard("")
        if len(unique) != 1:
            missing.append(name)
    return missing


def method_two_block(missing: list[str], constituents_ready: bool) -> str:
    if constituents_ready and not missing:
        return ""
    parts = ["Method 2 cannot identify the top two sectors."]
    if not constituents_ready:
        parts.append("One or more official sector constituent files are not usable.")
    if missing:
        parts.append("No single verified Groww index for: " + ", ".join(missing) + ".")
        parts.append("The current Groww instrument file has no Nifty Energy index and no Nifty India Defence index." if set(missing) >= {"Energy", "India Defence"} else "No substitute index was invented.")
    parts.append("No sector ranking was estimated.")
    return " ".join(parts)


def data_source_rows(permissions: dict | None, firebase: dict | None, sector_block: str) -> list[dict]:
    """Dashboard status for each required source. A saved probe is not treated as live data."""
    by_name = {row.get("name"): row for row in (permissions or {}).get("results") or []}
    tested_at = (permissions or {}).get("at") or "never"

    def from_probe(label, probe_name, action):
        row = by_name.get(probe_name)
        if not row:
            return {"Source": label, "State": "Not configured", "Last test": tested_at, "Required action": action}
        if row.get("ok"):
            state = "Connected"
            action = ""
        elif row.get("category") == "permission_denied":
            state = "Permission denied"
        else:
            state = "Failed"
        return {
            "Source": label,
            "State": state,
            "Last test": tested_at,
            "Required action": action if not row.get("ok") else "",
            "Detail": row.get("message") or "",
        }

    firebase = firebase or {}
    if firebase.get("ok"):
        firebase_row = {"Source": "Firebase", "State": "Connected", "Last test": firebase.get("at") or "saved", "Required action": ""}
    elif firebase.get("message"):
        firebase_row = {"Source": "Firebase", "State": "Failed", "Last test": firebase.get("at") or "saved", "Required action": "Check the service-account file and Firestore access.", "Detail": firebase.get("message")}
    else:
        firebase_row = {"Source": "Firebase", "State": "Not configured", "Last test": "never", "Required action": "Save the Firebase service-account file in Settings."}
    sector_state = "Blocked" if sector_block else "Connected"
    return [
        from_probe("Authentication", "Authentication", "Save a Groww API key and secret, or a TOTP token."),
        from_probe("Instrument access", "Instrument master", "Confirm the Groww account can download the instrument master."),
        from_probe("Live price access", "Live LTP", "Confirm the account is allowed to read live prices."),
        from_probe("Historical candle access", "Historical 5-minute candles", "Confirm the account is allowed to read historical candles."),
        from_probe("OI access", "Open interest", "Confirm the account is allowed to read F&O quotes."),
        firebase_row,
        {
            "Source": "NSE-wide ranking provider",
            "State": "Not configured",
            "Last test": "never",
            "Required action": "Provide an authorized NSE-wide gainer and loser feed. Groww does not publish one.",
        },
        {
            "Source": "Sector-index provider",
            "State": "Failed" if sector_block else "Connected",
            "Last test": "instrument file" if sector_block else tested_at,
            "Required action": sector_block or "",
        },
    ]
