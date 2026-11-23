"""Official Groww SDK wrapper.

This module reads market data only. It does not place, modify or cancel orders.
Missing fields stay missing. They are never filled with estimated prices.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from nse_scanner.rate_limit import RateLimiter
from nse_scanner.redact import public_text
from nse_scanner.secrets import load_secrets


class GrowwClient:
    def __init__(self, settings: dict, sleep=time.sleep):
        self.settings = settings
        self.sleep = sleep
        self.api = None
        self.feed = None
        self.last_error = ""
        self.last_success = None
        self.live_limiter = RateLimiter(
            settings.get("live_requests_per_second", 8),
            settings.get("live_requests_per_minute", 240),
            sleep=sleep,
        )
        self.history_limiter = RateLimiter(
            settings.get("history_requests_per_second", 4),
            settings.get("history_requests_per_minute", 120),
            sleep=sleep,
        )
        self.api_failures = 0

    def authenticate(self) -> tuple[bool, str]:
        secrets = load_secrets()
        api_key = secrets.get("groww_api_key") or ""
        if not api_key:
            return False, "Groww API key is not configured."
        try:
            from growwapi import GrowwAPI
            import pyotp
        except ImportError as exc:
            return False, f"Groww SDK is not installed: {exc}"
        mode = self.settings.get("groww_auth_mode", "api_key_secret")
        try:
            if mode == "totp":
                totp_secret = secrets.get("groww_totp_secret") or ""
                if not totp_secret:
                    return False, "Groww TOTP secret is not configured."
                token = GrowwAPI.get_access_token(api_key=api_key, totp=pyotp.TOTP(totp_secret).now())
            else:
                secret = secrets.get("groww_api_secret") or ""
                if not secret:
                    return False, "Groww API secret is not configured."
                token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
            if not isinstance(token, str) or not token:
                return False, "Groww did not return an access token."
            self.api = GrowwAPI(token)
            self.last_success = datetime.now().isoformat(timespec="seconds")
            return True, "Groww authentication succeeded."
        except Exception as exc:
            self.api_failures += 1
            self.last_error = public_text(str(exc))
            return False, self.last_error

    def _call(self, limiter: RateLimiter, method_name: str, **kwargs):
        if self.api is None:
            raise RuntimeError("Groww is not authenticated.")
        delay = 1.0
        last_exc = None
        for _ in range(4):
            limiter.wait()
            try:
                method = getattr(self.api, method_name)
                result = method(**kwargs)
                self.last_success = datetime.now().isoformat(timespec="seconds")
                return result
            except Exception as exc:
                last_exc = exc
                self.api_failures += 1
                self.last_error = public_text(str(exc))
                name = exc.__class__.__name__
                text = self.last_error.lower()
                if any(phrase in text for phrase in ("forbidden", "permission", "not subscribed", "entitlement")):
                    raise
                if name == "GrowwAPIAuthenticationException":
                    ok, _message = self.authenticate()
                    if not ok:
                        raise
                    continue
                if name not in {"GrowwAPIRateLimitException", "GrowwAPITimeoutException"}:
                    raise
                self.sleep(delay)
                delay = min(delay * 2, 8)
        raise last_exc

    def test_connection(self) -> tuple[bool, str]:
        ok, message = self.authenticate()
        if not ok:
            return ok, message
        try:
            payload = self._call(
                self.live_limiter,
                "get_ltp",
                segment=self.api.SEGMENT_CASH,
                exchange_trading_symbols=("NSE_NIFTY",),
            )
        except Exception as exc:
            return False, public_text(str(exc))
        if not isinstance(payload, dict) or "NSE_NIFTY" not in payload:
            return False, "Groww answered, but the Nifty LTP field was missing. No price was invented."
        return True, "Groww authentication and live LTP check succeeded."

    def probe_permissions(self) -> list[dict]:
        """Check each Groww capability on its own. Authentication is not a live-data entitlement."""
        names = [
            "Instrument master",
            "Live LTP",
            "Live quote",
            "Historical 5-minute candles",
            "Open interest",
            "Streaming feed",
        ]
        ok, message = self.authenticate()
        results = [{
            "name": "Authentication",
            "ok": ok,
            "category": "authentication" if ok else classify_probe_failure(message),
            "message": message,
        }]
        if not ok:
            for name in names:
                results.append({
                    "name": name,
                    "ok": False,
                    "category": "not_tested",
                    "message": "Not tested because authentication failed.",
                })
            return results
        instrument_result, frame = self._probe_instruments()
        results.append(instrument_result)
        results.append(self._probe_ltp())
        results.append(self._probe_quote())
        results.append(self._probe_history())
        results.append(self._probe_oi(frame))
        results.append(self._probe_feed(frame))
        return results

    def _probe_instruments(self):
        try:
            frame = self.download_instruments()
            rows = len(frame)
            if rows <= 0 or "trading_symbol" not in getattr(frame, "columns", []):
                return _probe("Instrument master", False, "request_failed", "Groww returned an instrument file without a trading symbol column."), None
            return _probe("Instrument master", True, "success", f"Instrument master returned {rows} rows. This is not a live price."), frame
        except Exception as exc:
            return _failed("Instrument master", exc), None

    def _probe_ltp(self) -> dict:
        try:
            payload = self._call(
                self.live_limiter, "get_ltp",
                segment=self.api.SEGMENT_CASH, exchange_trading_symbols=("NSE_RELIANCE",),
            )
        except Exception as exc:
            return _failed("Live LTP", exc)
        if not isinstance(payload, dict) or payload.get("NSE_RELIANCE") is None:
            return _probe("Live LTP", False, "request_failed", "Groww answered without an LTP for RELIANCE. No price was invented.")
        return _probe("Live LTP", True, "success", "Live LTP returned a value for RELIANCE.")

    def _probe_quote(self) -> dict:
        try:
            payload = self.get_quote("RELIANCE", segment="CASH")
        except Exception as exc:
            return _failed("Live quote", exc)
        price_fields = ("last_price", "ltp", "day_change", "open", "high", "low", "close", "ohlc")
        has_price = isinstance(payload, dict) and any(payload.get(field) not in (None, "") for field in price_fields)
        if not has_price:
            return _probe("Live quote", False, "request_failed", "Groww returned a quote without a last price. No price was invented.")
        return _probe("Live quote", True, "success", "Live quote returned for RELIANCE.")

    def _probe_history(self) -> dict:
        end = datetime.now().replace(microsecond=0)
        start = end - timedelta(days=5)
        try:
            interval = self.five_minute_interval()
            payload = self.historical_candles("NSE-RELIANCE", start, end, interval)
        except Exception as exc:
            return _failed("Historical 5-minute candles", exc)
        candles = payload.get("candles") if isinstance(payload, dict) else None
        if not candles:
            return _probe(
                "Historical 5-minute candles", False, "request_failed",
                "Groww returned no 5-minute candles for RELIANCE. No candle was invented.",
            )
        return _probe("Historical 5-minute candles", True, "success", f"Historical 5-minute candles returned {len(candles)} rows.")

    def _probe_oi(self, frame) -> dict:
        if frame is None:
            return _probe("Open interest", False, "not_tested", "Not tested because the instrument master was not available.")
        try:
            from nse_scanner.universe import nearest_futures
            futures = nearest_futures(frame, ["RELIANCE"], datetime.now().strftime("%Y-%m-%d"))
        except Exception as exc:
            return _failed("Open interest", exc)
        future = futures.get("RELIANCE")
        if not future:
            return _probe("Open interest", False, "request_failed", "No current RELIANCE future was found, so OI was not validated.")
        try:
            quote = self.get_quote(future["trading_symbol"], segment="FNO")
        except Exception as exc:
            return _failed("Open interest", exc)
        if not isinstance(quote, dict) or "oi_day_change" not in quote or quote.get("oi_day_change") is None:
            return _probe(
                "Open interest", False, "request_failed",
                "The future quote did not include oi_day_change. OI ranking stays unavailable.",
            )
        return _probe("Open interest", True, "success", "oi_day_change was present on the nearest RELIANCE future. OI ranking still stays off until its formula is approved.")

    def _probe_feed(self, frame) -> dict:
        token = None
        if frame is not None:
            try:
                cash = frame[
                    (frame["exchange"].astype(str).str.upper() == "NSE")
                    & (frame["segment"].astype(str).str.upper() == "CASH")
                    & (frame["trading_symbol"].astype(str).str.upper() == "RELIANCE")
                ]
                if len(cash):
                    token = str(cash.iloc[0].get("exchange_token") or "")
            except Exception:
                token = None
        if not token:
            return _probe("Streaming feed", False, "not_tested", "Not tested because RELIANCE had no exchange token in the instrument master.")
        ok, message = self.start_feed([{"exchange_token": token, "symbol": "RELIANCE"}])
        self.feed = None
        if not ok:
            return _failed("Streaming feed", RuntimeError(message))
        return _probe("Streaming feed", True, "success", message + " No tick was stored as a live price by this check.")

    def download_instruments(self):
        """Download the Groww instrument file in memory.

        The SDK writes instruments.csv inside its own package directory. That
        directory is read-only on Streamlit Cloud, so the file is parsed here
        and never saved beside the installed package.
        """
        if self.api is None:
            ok, message = self.authenticate()
            if not ok:
                raise RuntimeError(message)
        import io

        import pandas as pd
        import requests

        self.history_limiter.wait()
        try:
            response = requests.get(
                "https://growwapi-assets.groww.in/instruments/instrument.csv",
                timeout=60,
            )
            response.raise_for_status()
            frame = pd.read_csv(io.BytesIO(response.content), dtype="str")
        except Exception as exc:
            self.api_failures += 1
            self.last_error = public_text(str(exc))
            raise
        self.last_success = datetime.now().isoformat(timespec="seconds")
        return frame

    def get_ltp_batch(self, exchange_symbols: list[str]) -> dict:
        prices = {}
        for start in range(0, len(exchange_symbols), 50):
            chunk = tuple(exchange_symbols[start:start + 50])
            if not chunk:
                continue
            payload = self._call(
                self.live_limiter,
                "get_ltp",
                segment=self.api.SEGMENT_CASH,
                exchange_trading_symbols=chunk,
            )
            if isinstance(payload, dict):
                prices.update(payload)
        return prices

    def get_quote(self, trading_symbol: str, segment: str = "CASH") -> dict:
        segment_value = self.api.SEGMENT_FNO if segment == "FNO" else self.api.SEGMENT_CASH
        payload = self._call(
            self.live_limiter,
            "get_quote",
            trading_symbol=trading_symbol,
            exchange=self.api.EXCHANGE_NSE,
            segment=segment_value,
        )
        return payload if isinstance(payload, dict) else {}

    def get_ohlc_batch(self, exchange_symbols: list[str]) -> dict:
        combined = {}
        for start in range(0, len(exchange_symbols), 50):
            chunk = tuple(exchange_symbols[start:start + 50])
            payload = self._call(
                self.live_limiter,
                "get_ohlc",
                segment=self.api.SEGMENT_CASH,
                exchange_trading_symbols=chunk,
            )
            if isinstance(payload, dict):
                combined.update(payload)
        return combined

    def historical_candles(self, groww_symbol: str, start: datetime, end: datetime, interval: str, segment: str = "CASH") -> dict:
        segment_value = self.api.SEGMENT_FNO if segment == "FNO" else self.api.SEGMENT_CASH
        return self._call(
            self.history_limiter,
            "get_historical_candles",
            exchange=self.api.EXCHANGE_NSE,
            segment=segment_value,
            groww_symbol=groww_symbol,
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
            candle_interval=interval,
        )

    def five_minute_interval(self) -> str:
        value = getattr(self.api, "CANDLE_INTERVAL_MIN_5", None)
        if value != "5minute":
            raise RuntimeError("The installed Groww SDK does not expose the documented 5-minute candle interval.")
        return value

    def daily_interval(self) -> str:
        value = getattr(self.api, "CANDLE_INTERVAL_DAY", None)
        if value != "1day":
            raise RuntimeError("The installed Groww SDK does not expose the documented daily candle interval.")
        return value

    def previous_day_window(self, trading_day: datetime) -> tuple[datetime, datetime]:
        start = trading_day - timedelta(days=12)
        end = trading_day - timedelta(seconds=1)
        return start, end

    def start_feed(self, instruments: list[dict]) -> tuple[bool, str]:
        if len(instruments) > 1000:
            self.feed = None
            return False, "More than 1000 instruments were requested. The live feed was not partially subscribed."
        try:
            from growwapi import GrowwFeed
            _redirect_groww_feed_files()
            self.feed = GrowwFeed(self.api)
            subscription = [
                {"exchange": "NSE", "segment": "CASH", "exchange_token": str(item["exchange_token"])}
                for item in instruments
                if item.get("exchange_token")
            ]
            if not subscription:
                return False, "No exchange tokens are available for the live feed."
            self.feed.subscribe_ltp(subscription)
            return True, f"Live feed subscribed to {len(subscription)} instruments."
        except Exception as exc:
            self.feed = None
            self.api_failures += 1
            self.last_error = public_text(str(exc))
            return False, self.last_error

    def feed_prices(self) -> dict:
        if self.feed is None:
            return {}
        try:
            payload = self.feed.get_ltp() or {}
            return payload.get("ltp", payload) if isinstance(payload, dict) else {}
        except Exception as exc:
            self.last_error = public_text(str(exc))
            self.feed = None
            return {}

    def reconnect_feed(self, instruments: list[dict]) -> tuple[bool, str]:
        self.feed = None
        return self.start_feed(instruments)


def _redirect_groww_feed_files() -> None:
    """Store the feed credential files outside the Groww package.

    The SDK writes a.creds and b.creds beside its own source. Streamlit Cloud
    cannot write inside site-packages, so those files go in the app home.
    """
    import tempfile
    from pathlib import Path

    from growwapi.common import files as file_utils

    from nse_scanner.paths import secrets_dir

    def folder() -> Path:
        try:
            path = secrets_dir() / "groww_feed"
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            path = Path(tempfile.gettempdir()) / "nse_fno_groww_feed"
            path.mkdir(parents=True, exist_ok=True)
            return path

    def write(name: str, content: str) -> str:
        path = folder() / name
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return str(path)

    file_utils.generate_token_file = lambda jwt_token: write("a.creds", str(jwt_token))
    file_utils.generate_seed_file = lambda seed: write("b.creds", str(seed))


def classify_probe_failure(message: str) -> str:
    text = (message or "").lower()
    if any(phrase in text for phrase in ("not configured", "did not return an access token", "sdk is not installed")):
        return "authentication"
    if "errno 13" in text or "site-packages" in text or "instruments.csv" in text:
        return "request_failed"
    if any(phrase in text for phrase in ("forbidden", "permission", "authorisation failed", "authorization failed", "not subscribed", "entitlement")):
        return "permission_denied"
    return "request_failed"


def _probe(name: str, ok: bool, category: str, message: str) -> dict:
    return {"name": name, "ok": ok, "category": category, "message": message}


def _failed(name: str, exc: Exception) -> dict:
    message = public_text(str(exc))
    return _probe(name, False, classify_probe_failure(message), message)
