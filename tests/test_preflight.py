from datetime import datetime

import pandas as pd

from nse_scanner.preflight import format_report, market_session_open, run_preflight, save_report, status_from_error
from scanner_rules import IST


class FakeClient:
    def __init__(self, auth=(True, "Groww authentication succeeded."), quote=None, error=None):
        self.auth = auth
        self.quote = quote if quote is not None else {"last_price": 100, "oi_day_change": 5, "open_interest": 10}
        self.error = error

    def authenticate(self):
        return self.auth

    def download_instruments(self):
        return pd.DataFrame([
            {"exchange": "NSE", "segment": "CASH", "trading_symbol": "RELIANCE", "series": "EQ", "instrument_type": "EQ",
             "underlying_symbol": None, "name": "Reliance", "exchange_token": "1", "groww_symbol": "NSE-RELIANCE"},
            {"exchange": "NSE", "segment": "FNO", "trading_symbol": "RELIANCE25NOVFUT", "series": None, "instrument_type": "FUT",
             "underlying_symbol": "RELIANCE", "expiry_date": "2026-11-26", "exchange_token": "2", "groww_symbol": "NSE-RELIANCE-FUT"},
            {"exchange": "NSE", "segment": "CASH", "trading_symbol": "NIFTYREALTY", "series": "INDEX", "instrument_type": "IDX",
             "underlying_symbol": None, "name": "Nifty Realty", "exchange_token": "3", "groww_symbol": "NSE-NIFTYREALTY"},
        ])

    def get_quote(self, symbol, segment="CASH"):
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.quote)

    def five_minute_interval(self):
        return "5minute"

    def daily_interval(self):
        return "1day"

    def previous_day_window(self, trading_day):
        return trading_day, trading_day

    def historical_candles(self, groww_symbol, start, end, interval, segment="CASH"):
        return {"candles": [["2026-11-20 15:25:00", 100, 101, 99, 100.5, 1000]]}


class FakeStore:
    def __init__(self):
        self.document_id = ""

    def preflight_roundtrip(self, document_id):
        self.document_id = document_id
        assert document_id.startswith("preflight_")
        return True, f"Firestore wrote, read, and deleted api_health/{document_id}."


def test_status_labels_keep_authentication_and_permission_apart():
    assert status_from_error("Groww API key is not configured.") == "NOT CONFIGURED"
    assert status_from_error("Access forbidden for this request") == "PERMISSION DENIED"
    assert status_from_error("timed out") == "FAIL"


def test_weekend_preflight_does_not_call_a_returned_price_a_live_quote():
    now = datetime(2026, 11, 21, 17, 0, tzinfo=IST)
    assert market_session_open(now) is False
    store = FakeStore()
    report = run_preflight(now=now, client=FakeClient(), store=store, settings={"constituent_max_age_hours": 168})
    by_name = {item["name"]: item for item in report["checks"]}
    assert by_name["Groww authentication"]["status"] == "PASS"
    assert by_name["F&O stock quote"]["status"] == "MARKET DATA UNAVAILABLE"
    assert "not a verified live quote" in by_name["F&O stock quote"]["details"]
    assert by_name["Method 3 NSE-wide ranking"]["status"] == "NOT CONFIGURED"
    assert by_name["Method 2 sector ranking"]["status"] == "MARKET DATA UNAVAILABLE"
    assert "Energy" in by_name["Method 2 sector ranking"]["details"]
    assert by_name["OI-spurt rule"]["status"] == "NOT CONFIGURED"
    assert report["overall"] == "BLOCKED"
    assert store.document_id.startswith("preflight_")
    text = format_report(report)
    assert "No BUY or SHORT signal was generated." in text
    assert "api_key" not in text
    assert "secret" not in text.lower()


def test_permission_denied_is_not_reported_as_a_failed_login():
    now = datetime(2026, 11, 21, 17, 0, tzinfo=IST)
    client = FakeClient(error="Access forbidden for this request")
    report = run_preflight(now=now, client=client, store=FakeStore(), settings={})
    quote = next(item for item in report["checks"] if item["name"] == "F&O stock quote")
    assert report["checks"][0]["status"] == "PASS"
    assert quote["status"] == "PERMISSION DENIED"


def test_missing_credentials_stop_before_a_market_call():
    now = datetime(2026, 11, 21, 17, 0, tzinfo=IST)
    client = FakeClient(auth=(False, "Groww API key is not configured."))
    called = {"quote": False}
    client.get_quote = lambda *args, **kwargs: called.__setitem__("quote", True)
    report = run_preflight(now=now, client=client, store=FakeStore(), settings={})
    assert report["checks"][0]["status"] == "NOT CONFIGURED"
    assert called["quote"] is False
    assert report["overall"] == "BLOCKED"


def test_saved_report_has_no_secret_material():
    now = datetime(2026, 11, 21, 17, 0, tzinfo=IST)
    report = run_preflight(now=now, client=FakeClient(), store=FakeStore(), settings={})
    path = save_report(report)
    text = open(path, encoding="utf-8").read()
    assert "Overall readiness" in text
    assert "BLOCKED" in text
    assert "api_key" not in text
