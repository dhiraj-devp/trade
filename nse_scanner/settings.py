"""Non-secret application settings. Strategy defaults stay in force until approved."""

from __future__ import annotations

import json
from copy import deepcopy

from nse_scanner.paths import app_home
from scanner_rules import (
    ALTERNATE_BREADTH_DENOMINATOR,
    APPROVED_BREADTH_DENOMINATOR,
    DEFAULT_ADVANCE_MIN,
    DEFAULT_CANDLE_MINUTES,
    DEFAULT_CLOSE,
    DEFAULT_COMBINED_MOVE,
    DEFAULT_DECLINE_MAX,
    DEFAULT_MAX_ABS_MOVE,
    DEFAULT_OPEN,
    DEFAULT_SECOND_MOVE,
    DEFAULT_SECTOR_COUNT,
    DEFAULT_SELECTION_DEADLINE,
    DEFAULT_SETUP3_END,
    DEFAULT_STOCKS_PER_SECTOR,
    DEFAULT_TOP_N,
    SECTOR_NAMES,
)

CONFIG_NAME = "settings.json"

DEFAULTS = {
    "timezone": "Asia/Kolkata",
    "market_open": DEFAULT_OPEN.strftime("%H:%M"),
    "market_close": DEFAULT_CLOSE.strftime("%H:%M"),
    "selection_deadline": DEFAULT_SELECTION_DEADLINE.strftime("%H:%M"),
    "setup3_end": DEFAULT_SETUP3_END.strftime("%H:%M"),
    "candle_minutes": DEFAULT_CANDLE_MINUTES,
    "max_abs_move": DEFAULT_MAX_ABS_MOVE,
    "advance_min": DEFAULT_ADVANCE_MIN,
    "decline_max": DEFAULT_DECLINE_MAX,
    "setup2_combined_pct": DEFAULT_COMBINED_MOVE,
    "setup2_second_pct": DEFAULT_SECOND_MOVE,
    "breadth_denominator": APPROVED_BREADTH_DENOMINATOR,
    "breadth_denominator_approved": False,
    "core_strategy_change_approved": False,
    "price_movers_enabled": True,
    "nse_rank_enabled": True,
    "oi_enabled": False,
    "oi_definition_approved": False,
    "oi_definition": "nearest_expiry_stock_future_oi_day_change",
    "top_n": DEFAULT_TOP_N,
    "sector_enabled": True,
    "sector_choice_saved": False,
    "sector_source": "official_constituents",
    "sector_method": "official_index",
    "sector_method_approved": False,
    "sector_count": DEFAULT_SECTOR_COUNT,
    "stocks_per_sector": DEFAULT_STOCKS_PER_SECTOR,
    "sector_symbols": {name: "" for name in SECTOR_NAMES},
    "sector_constituents_approved": False,
    "tie_break_approved": False,
    "nifty50_approved": False,
    "nifty50_hash": "",
    "fno_universe_approved": False,
    "fno_universe_hash": "",
    "constituent_max_age_hours": 168,
    "constituent_refresh_hours": 12,
    "setup3_include_cutoff_candle": False,
    "setup3_cutoff_change_approved": True,
    "setup3_include_1100_approved": False,
    "breadth_reversal_protection": False,
    "breadth_reversal_protection_change_approved": False,
    "quote_max_age_seconds": 30,
    "live_requests_per_second": 8,
    "live_requests_per_minute": 240,
    "history_requests_per_second": 4,
    "history_requests_per_minute": 120,
    "telegram_enabled": False,
    "telegram_chat_id": "",
    "email_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_from": "",
    "smtp_to": "",
    "groww_auth_mode": "api_key_secret",
    "last_groww_test": {},
    "last_firebase_test": {},
    "wizard_seen": False,
    "developer_mode": False,
    "firestore_write_target": 2000,
}

CORE_FIELDS = (
    "market_open",
    "market_close",
    "selection_deadline",
    "setup3_end",
    "candle_minutes",
    "max_abs_move",
    "advance_min",
    "decline_max",
    "setup2_combined_pct",
    "setup2_second_pct",
    "top_n",
    "sector_count",
    "stocks_per_sector",
)


def config_path():
    app_home().mkdir(parents=True, exist_ok=True)
    return app_home() / CONFIG_NAME


def load_settings() -> dict:
    path = config_path()
    data = deepcopy(DEFAULTS)
    stored = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stored = {}
        if isinstance(stored, dict):
            data.update(stored)
        else:
            stored = {}
    data["timezone"] = "Asia/Kolkata"
    data["sector_symbols"] = {**{name: "" for name in SECTOR_NAMES}, **(data.get("sector_symbols") or {})}
    if data.get("breadth_denominator") not in (APPROVED_BREADTH_DENOMINATOR, ALTERNATE_BREADTH_DENOMINATOR):
        data["breadth_denominator"] = APPROVED_BREADTH_DENOMINATOR
    untouched_placeholder = (
        path.exists()
        and "sector_choice_saved" not in stored
        and not stored.get("sector_constituents_approved")
        and not stored.get("sector_method_approved")
    )
    if untouched_placeholder:
        data["sector_enabled"] = True
        data["sector_source"] = "official_constituents"
    return data


def save_settings(data: dict) -> dict:
    current = load_settings()
    current.update(data)
    current["timezone"] = "Asia/Kolkata"
    path = config_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, indent=2), encoding="utf-8")
    temporary.replace(path)
    return current


def core_changes(data: dict) -> dict:
    return {field: data.get(field) for field in CORE_FIELDS if data.get(field) != DEFAULTS[field]}


def strategy_is_approved(data: dict) -> tuple[bool, str]:
    changes = core_changes(data)
    if changes and not data.get("core_strategy_change_approved"):
        fields = ", ".join(sorted(changes))
        return False, f"Core strategy values differ from the approved defaults ({fields}) and have not been confirmed."
    if data.get("breadth_denominator") != APPROVED_BREADTH_DENOMINATOR and not data.get("breadth_denominator_approved"):
        return False, "The alternate breadth denominator includes unchanged stocks and has not been approved."
    if int(data.get("candle_minutes") or 0) != DEFAULT_CANDLE_MINUTES and not data.get("core_strategy_change_approved"):
        return False, "The candle interval is part of the strategy and must stay at 5 minutes unless the change is approved."
    if data.get("setup3_include_cutoff_candle") and not data.get("setup3_include_1100_approved"):
        return False, "Including the 11:00-11:05 candle is not the client rule. It stays excluded until that change is approved."
    if data.get("breadth_reversal_protection") and not data.get("breadth_reversal_protection_change_approved"):
        return False, "Blocking new signals after the locked breadth changes is an extra protection and is not approved."
    return True, "Strategy settings are at the approved defaults or an explicit change has been confirmed."


def developer_mode_enabled(settings: dict | None = None) -> bool:
    data = settings if settings is not None else load_settings()
    return bool(data.get("developer_mode"))


def session_limits_apply(settings: dict) -> bool:
    """Production keeps the market clock. Developer mode is for testing and ignores it."""
    return not developer_mode_enabled(settings)


def quote_max_age_seconds(settings: dict) -> float:
    age = float(settings.get("quote_max_age_seconds") or 30)
    if developer_mode_enabled(settings):
        return max(age, 86_400.0)
    return age


def public_settings() -> dict:
    data = load_settings()
    data.pop("last_groww_test", None)
    return data
