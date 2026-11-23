import json
import os
from pathlib import Path

from nse_scanner.settings import load_settings, quote_max_age_seconds, save_settings, session_limits_apply
from nse_scanner.state import read_status, write_json, write_status
from nse_scanner.store import scoped_collection


def test_developer_mode_is_off_unless_saved():
    assert load_settings()["developer_mode"] is False
    assert session_limits_apply(load_settings()) is True
    assert quote_max_age_seconds({"quote_max_age_seconds": 30}) == 30


def test_developer_mode_relaxes_only_its_own_clock():
    settings = {"developer_mode": True, "quote_max_age_seconds": 30}
    assert session_limits_apply(settings) is False
    assert quote_max_age_seconds(settings) == 86_400


def test_developer_records_do_not_replace_production_records():
    save_settings({"developer_mode": False})
    write_status({"scanner_state": "running", "message": "production scan"})
    write_json("selection_lock.json", {"locked": True, "reason": "production lock"})
    root = Path(os.environ["NSE_FNO_RUNTIME_DIR"])

    save_settings({"developer_mode": True})
    write_status({"scanner_state": "running", "message": "developer scan"})
    write_json("selection_lock.json", {"locked": True, "reason": "developer lock"})

    assert read_status()["message"] == "developer scan"
    developer_lock = json.loads((root / "developer" / "selection_lock.json").read_text(encoding="utf-8"))
    production_lock = json.loads((root / "selection_lock.json").read_text(encoding="utf-8"))
    assert developer_lock["reason"] == "developer lock"
    assert production_lock["reason"] == "production lock"

    save_settings({"developer_mode": False})
    assert read_status()["message"] == "production scan"
    assert "developer scan" not in (root / "status.json").read_text(encoding="utf-8")


def test_firebase_quota_is_recognized():
    from nse_scanner.store import cloud_is_limited
    assert cloud_is_limited("Timeout of 60.0s exceeded, last exception: 429 Quota exceeded.")
    assert cloud_is_limited("normal timeout") is False


def test_database_names_stay_on_separate_collections():
    assert scoped_collection("daily_candidates", False) == "daily_candidates"
    assert scoped_collection("scanner_signals", False) == "scanner_signals"
    assert scoped_collection("daily_candidates", True) == "dev_daily_candidates"
    assert scoped_collection("scanner_signals", True) == "dev_scanner_signals"
