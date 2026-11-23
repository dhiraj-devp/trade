"""Live-scan readiness. A failed check means the scanner must not pretend to be live."""

from __future__ import annotations

from nse_scanner.secrets import secret_status
from nse_scanner.settings import strategy_is_approved
from scanner_rules import SECTOR_NAMES


def readiness(settings: dict, universe_hash: str = "", nifty_symbols: list[str] | None = None,
              sector_file_ok: bool = False, nifty_meta: dict | None = None,
              sector_status: dict | None = None) -> dict:
    secrets = secret_status()
    checks = []

    def add(name: str, passed: bool, detail: str):
        checks.append({"name": name, "passed": passed, "detail": detail})

    add("Groww credentials", secrets["groww_api_key"] and (
        secrets["groww_api_secret"] if settings.get("groww_auth_mode") != "totp" else secrets["groww_totp_secret"]
    ), "API key and the selected authentication secret are stored outside the project.")
    groww_test = settings.get("last_groww_test") or {}
    add("Groww connection test", bool(groww_test.get("ok")), groww_test.get("message") or "Run Test connection in Settings.")
    add("Firebase credentials", secrets["firebase_service_account"], "Upload the Firebase service account in Settings.")
    firebase_test = settings.get("last_firebase_test") or {}
    add("Firebase connection test", bool(firebase_test.get("ok")), firebase_test.get("message") or "Run the Firestore test in Settings.")
    if nifty_meta is None:
        add("Nifty 50 list", bool(settings.get("nifty50_approved") and nifty_symbols), "Upload the current official Nifty 50 constituents and approve them.")
    else:
        add(
            "Nifty 50 list",
            bool(nifty_meta.get("usable") and nifty_symbols),
            nifty_meta.get("detail") or "The official Nifty 50 list is not usable.",
        )
    add(
        "F&O universe",
        bool(universe_hash),
        "NSE F&O underlyings that also have an NSE cash series EQ listing. The list comes from the Groww instrument file.",
    )
    approved, strategy_detail = strategy_is_approved(settings)
    add("Strategy confirmation", approved, strategy_detail)
    if settings.get("oi_enabled"):
        add("OI ranking", bool(settings.get("oi_definition_approved")), "OI ranking stays off until its definition is approved.")
    else:
        add("OI ranking", True, "OI ranking is disabled. Price gainers and losers remain the default.")
    if settings.get("sector_enabled"):
        if settings.get("sector_source") == "official_constituents":
            add(
                "Sector constituents",
                bool(sector_status and sector_status.get("ready")),
                (sector_status or {}).get("detail") or "One or more official sector constituent lists are not usable.",
            )
        else:
            add(
                "Sector constituents",
                bool(settings.get("sector_constituents_approved") and sector_file_ok),
                "Sector stock picks need a verified constituent file. Index moves alone do not identify the stocks.",
            )
            if settings.get("sector_method") == "official_index":
                symbols = settings.get("sector_symbols") or {}
                missing = [name for name in SECTOR_NAMES if not str(symbols.get(name) or "").strip()]
                add(
                    "Sector indexes",
                    bool(settings.get("sector_method_approved") and not missing),
                    "Every sector needs one approved Groww index symbol." if missing else "Sector index symbols are approved.",
                )
    else:
        add("Sector method", True, "Sector selection is turned off.")
    ready = all(item["passed"] for item in checks)
    return {"ready": ready, "checks": checks}


def scan_is_blocked(report: dict) -> bool:
    """Firebase being down does not stop the local scan. Market data checks still do."""
    for item in report.get("checks") or []:
        if str(item.get("name") or "").startswith("Firebase"):
            continue
        if not item.get("passed"):
            return True
    return False
