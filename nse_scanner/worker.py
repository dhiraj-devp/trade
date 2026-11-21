"""Background scanner. It keeps running when the dashboard is closed.

The process never places orders. It publishes only data that came from Groww
and rules that passed the market gate. Incomplete data produces no candidates.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd

from nse_scanner.candles import (
    history_is_complete,
    load_cache,
    merge_candles,
    parse_groww_candles,
    save_cache,
    store_previous_day,
)
from nse_scanner.control import clear_worker_record, stop_requested, write_worker_record
from nse_scanner.constituents import apply_cash_validation, describe_lists, refresh_official_lists
from nse_scanner.engine import (
    choose_candidates,
    effective_reversal_protection,
    lock_from_snapshot,
    parse_clock,
    quotes_from_prices,
    reversal_allows_signals,
    signals_for_candidate,
)
from nse_scanner.groww_client import GrowwClient
from nse_scanner.logging_setup import setup_logging
from nse_scanner.notify import notify_all
from nse_scanner.paths import lists_dir
from nse_scanner.readiness import readiness
from nse_scanner.redact import public_text
from nse_scanner.settings import load_settings, public_settings
from nse_scanner.state import read_json, write_json, write_status
from nse_scanner.store import FirestoreStore
from nse_scanner.universe import (
    cash_equities,
    derive_fno_equity_universe,
    find_index_matches,
    nearest_futures,
    stable_hash,
)
from nse_scanner.providers import method_two_block, sector_index_gap
from scanner_rules import IST, SECTOR_INDEX_SEARCH_KEYS, SECTOR_NAMES, assess_quote_group, change_pct, ist_now, market_from_groups


class Scanner:
    def __init__(self, client=None, store=None, clock=ist_now, sleep=time.sleep):
        self.client = client
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.logger = setup_logging()
        self.cache = None
        self.universe = []
        self.nifty = []
        self.indexes = []
        self.sector_map = {}
        self.list_report = {}
        self.index_matches = {}
        self.sector_block = "Sector indexes have not been checked against the Groww instrument file yet."
        self.known_ids = set()
        self.pending = None
        self.lock = None
        self.errors = []
        self.feed_status = "not started"
        self.last_report_date = read_json("report_date.json", {}).get("trading_date")

    def run(self) -> None:
        write_worker_record()
        self.logger.info("Scanner process started. Order placement is not implemented.")
        try:
            while not stop_requested():
                try:
                    self.cycle()
                except Exception as exc:
                    self.errors.append(public_text(str(exc)))
                    self.errors = self.errors[-20:]
                    self.logger.exception("Scanner cycle failed")
                    self.publish("error", public_text(str(exc)))
                self.sleep(5)
        finally:
            clear_worker_record()
            self.logger.info("Scanner process stopped.")

    def cycle(self) -> None:
        now = self.clock()
        settings = load_settings()
        trading_date = str(now.date())
        if now.weekday() >= 5:
            self.publish("closed", "Weekend. The NSE cash session is closed.", settings, now)
            return
        holiday = self._holiday(trading_date)
        if holiday:
            self.publish("closed", f"Exchange holiday ({holiday}). The live scan is not running.", settings, now)
            return
        if self.cache is None or self.cache.get("trading_date") != trading_date:
            self.cache = load_cache(trading_date)
            self.pending = self._load_pending(trading_date)
            self.lock = self._load_lock(trading_date)
        open_time = parse_clock(settings["market_open"])
        close_time = parse_clock(settings["market_close"])
        deadline = parse_clock(settings["selection_deadline"])
        if not self._connect(settings):
            return
        if not self._load_market_lists(settings, now):
            return
        self._load_known_signals(trading_date)
        if now.time() < open_time:
            self.publish("waiting", "Before the market open. Previous-day candles are loading.", settings, now)
            self._fill_previous_closes(now)
            self.publish("waiting", "Before the market open. Previous-day candles are loading.", settings, now)
            return
        if now.time() >= close_time:
            self._report(now, settings)
            self.publish("closed", "Market closed for the day.", settings, now)
            return
        self._fill_previous_closes(now)
        self._refresh_feed()
        prices, price_error = self._prices(now)
        if price_error:
            self.errors.append(price_error)
        self._refresh_candles(now, settings)
        market = self._breadth(settings, prices, now)
        notes = []
        if self.lock is None and now.time() <= deadline and market.get("complete"):
            age = None if not self.pending else (now - self.pending["taken_at"]).total_seconds()
            if age is None or age >= 60:
                self.pending = self._snapshot(settings, market, prices, now)
            notes.append("A fresh pre-deadline snapshot is ready.")
        if self.lock is None and now.time() >= deadline:
            snapshot = self._pending_for_lock()
            locked, message = lock_from_snapshot(
                snapshot, now, deadline, float(settings.get("quote_max_age_seconds") or 30)
            )
            self._store_lock(locked, message, trading_date, now)
            notes.append(message)
        if self.lock and self.lock.get("direction"):
            allowed, message = reversal_allows_signals(
                self.lock.get("direction"), market, effective_reversal_protection(settings)
            )
            notes.append(message)
            if allowed:
                notes.extend(self._emit(now, settings))
        self.publish("running", " ".join(notes) or "Scanner is running.", settings, now, market=market, prices=prices)

    def _connect(self, settings) -> bool:
        report = readiness(settings, universe_hash=settings.get("fno_universe_hash") or "pending",
                           nifty_symbols=["pending"] if settings.get("nifty50_approved") else [],
                           sector_file_ok=(lists_dir() / "sectors.csv").exists())
        # Connection itself is checked here; universe approval is checked after the master loads.
        if not report["checks"][0]["passed"] or not report["checks"][2]["passed"]:
            self.publish("not_ready", "Groww and Firebase credentials are required before live scanning.", settings, self.clock())
            return False
        if self.client is None:
            self.client = GrowwClient(settings, sleep=self.sleep)
        if self.client.api is None:
            ok, message = self.client.authenticate()
            if not ok:
                self.publish("not_ready", message, settings, self.clock())
                return False
        if self.store is None:
            try:
                self.store = FirestoreStore.connect()
                self.store.save_public_config(public_settings())
            except Exception as exc:
                self.publish("not_ready", public_text(str(exc)), settings, self.clock())
                return False
        last_ping = getattr(self, "last_ping", None)
        if last_ping is None or (self.clock() - last_ping).total_seconds() >= 60:
            ok, message = self.store.ping()
            self.last_ping = self.clock()
            if not ok:
                self.publish("not_ready", message, settings, self.clock())
                return False
        return True

    def _load_market_lists(self, settings, now) -> bool:
        try:
            frame = self._instrument_frame()
            self.universe = derive_fno_equity_universe(frame)
            cash = cash_equities(frame)
        except Exception as exc:
            self.publish("not_ready", public_text(str(exc)), settings, now)
            return False
        universe_hash = stable_hash([item["symbol"] for item in self.universe])
        write_json("fno_universe.json", {
            "generated_at": now.isoformat(sep=" ", timespec="seconds"),
            "hash": universe_hash,
            "count": len(self.universe),
            "symbols": self.universe,
            "rule": "NSE F&O underlyings that also have an NSE cash instrument in series EQ.",
        })
        self.list_report = refresh_official_lists(
            now,
            max_age_hours=float(settings.get("constituent_max_age_hours") or 168),
            refresh_hours=float(settings.get("constituent_refresh_hours") or 12),
        )
        self.list_report = apply_cash_validation(self.list_report, set(cash))
        nifty_meta = self.list_report.get("nifty50") or {}
        nifty_symbols = list(nifty_meta.get("symbols") or [])
        self.nifty = [cash[symbol] for symbol in nifty_symbols if symbol in cash]
        self.sector_map = dict(self.list_report.get("sector_symbols") or {})
        sector_status = self.list_report.get("sectors") or {}
        self._resolve_indexes(frame)
        report = readiness(
            settings, universe_hash, nifty_symbols, bool(self.sector_map),
            nifty_meta=nifty_meta, sector_status=sector_status,
        )
        if not report["ready"]:
            self.publish("not_ready", "Live scanning is blocked until every mandatory check passes.", settings, now, readiness_report=report)
            return False
        self.store.upsert_instruments(self.nifty + self.universe + self.indexes)
        return True

    def _instrument_frame(self) -> pd.DataFrame:
        from nse_scanner.paths import runtime_dir
        path = runtime_dir() / "instruments.csv"
        if not path.exists() or datetime.fromtimestamp(path.stat().st_mtime) < datetime.now() - timedelta(hours=18):
            frame = self.client.download_instruments()
            frame.to_csv(path, index=False)
            return frame
        return pd.read_csv(path)

    def _holiday(self, trading_date: str) -> str:
        path = lists_dir() / "holidays.csv"
        if not path.exists():
            return ""
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError):
            return ""
        column = next((name for name in frame.columns if str(name).strip().lower() == "date"), None)
        if column is None:
            return ""
        for value in frame[column].tolist():
            if str(value).strip()[:10] == trading_date:
                return trading_date
        return ""

    def _resolve_indexes(self, frame) -> None:
        matches = find_index_matches(frame, SECTOR_INDEX_SEARCH_KEYS)
        self.indexes = []
        self.index_matches = {}
        missing, ambiguous = [], []
        for sector in SECTOR_NAMES:
            unique = {}
            for match in matches.get(sector, []):
                unique[match["trading_symbol"]] = match
            self.index_matches[sector] = sorted(unique)
            if len(unique) == 1:
                match = next(iter(unique.values()))
                self.indexes.append({
                    "symbol": match["trading_symbol"],
                    "name": match.get("name") or sector,
                    "exchange_token": match.get("exchange_token"),
                    "groww_symbol": match.get("groww_symbol") or f"NSE-{match['trading_symbol']}",
                    "sector": sector,
                    "segment": "CASH",
                })
            elif not unique:
                missing.append(sector)
            else:
                ambiguous.append(sector)
        parts_missing = sector_index_gap(self.index_matches)
        constituents_ready = len(self.sector_map) == len(SECTOR_NAMES)
        self.sector_block = method_two_block(parts_missing, constituents_ready)

    def _fill_previous_closes(self, now, limit: int = 20) -> None:
        start, end = self.client.previous_day_window(datetime.combine(now.date(), datetime.min.time()))
        interval = self.client.daily_interval()
        attempts = self.cache.setdefault("previous_attempts", {})
        fetched = 0
        for item in self._monitored():
            symbol = item["symbol"]
            if symbol in self.cache["previous"]:
                continue
            last_try = attempts.get(symbol)
            if last_try and (now - datetime.strptime(last_try, "%Y-%m-%d %H:%M:%S")).total_seconds() < 600:
                continue
            if stop_requested() or fetched >= limit:
                break
            attempts[symbol] = now.strftime("%Y-%m-%d %H:%M:%S")
            try:
                payload = self.client.historical_candles(item["groww_symbol"], start, end, interval)
                store_previous_day(self.cache, symbol, parse_groww_candles(payload), str(now.date()))
            except Exception as exc:
                self.errors.append(f"{symbol} previous day: {public_text(str(exc))}")
            fetched += 1
        save_cache(self.cache)

    def _refresh_feed(self) -> None:
        if self.client.feed is not None:
            self.feed_status = "subscribed"
            return
        instruments = self._monitored()
        if len(instruments) > 1000:
            self.feed_status = "over the 1000 subscription limit; using batched LTP"
            return
        ok, message = self.client.start_feed(instruments)
        self.feed_status = "subscribed" if ok else f"REST fallback: {message}"

    def _prices(self, now) -> tuple[dict, str]:
        token_to_symbol = {
            str(item.get("exchange_token")): item["symbol"]
            for item in self._monitored() if item.get("exchange_token")
        }
        prices = {}
        raw = self.client.feed_prices()
        cash = raw.get("NSE", {}).get("CASH", {}) if isinstance(raw, dict) else {}
        for token, row in cash.items() if isinstance(cash, dict) else []:
            symbol = token_to_symbol.get(str(token))
            if not symbol or not isinstance(row, dict) or row.get("ltp") is None or row.get("tsInMillis") is None:
                continue
            as_of = datetime.fromtimestamp(float(row["tsInMillis"]) / 1000, IST).replace(tzinfo=None)
            prices[symbol] = (float(row["ltp"]), as_of)
        missing = [f"NSE_{item['symbol']}" for item in self._monitored() if item["symbol"] not in prices]
        if not missing:
            return prices, ""
        try:
            payload = self.client.get_ltp_batch(missing)
        except Exception as exc:
            return prices, public_text(str(exc))
        for key, value in payload.items():
            if value is None or "_" not in str(key):
                continue
            prices[str(key).split("_", 1)[1]] = (float(value), now)
        return prices, ""

    def _refresh_candles(self, now, settings) -> None:
        interval = self.client.five_minute_interval()
        start = datetime.combine(now.date(), parse_clock(settings["market_open"]))
        targets = self.universe if self.lock is None else [
            item for item in self.universe if item["symbol"] in {row["symbol"] for row in self.lock.get("candidates") or []}
        ]
        fetched = self.cache.setdefault("fetched_at", {})
        for item in targets:
            complete, _missing = history_is_complete(
                self.cache, item["symbol"], now.date(), now,
                open_time=parse_clock(settings["market_open"]),
                close_time=parse_clock(settings["market_close"]),
                interval_minutes=int(settings["candle_minutes"]),
            )
            if complete:
                continue
            last = fetched.get(item["symbol"])
            if last and (now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() < 60:
                continue
            try:
                payload = self.client.historical_candles(item["groww_symbol"], start, now, interval)
                rows = [row for row in parse_groww_candles(payload) if row["ts"][:10] == str(now.date())]
                merge_candles(self.cache, item["symbol"], rows)
                fetched[item["symbol"]] = now.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                self.errors.append(f"{item['symbol']} candles: {public_text(str(exc))}")
        save_cache(self.cache)

    def _breadth(self, settings, prices, now) -> dict:
        max_age = float(settings.get("quote_max_age_seconds") or 30)
        thresholds = {
            "advance_min": float(settings["advance_min"]),
            "decline_max": float(settings["decline_max"]),
            "denominator": settings["breadth_denominator"],
        }
        nifty = assess_quote_group(quotes_from_prices(self.nifty, prices, self.cache["previous"], now),
                                   [item["symbol"] for item in self.nifty], now, max_age)
        fno = assess_quote_group(quotes_from_prices(self.universe, prices, self.cache["previous"], now),
                                 [item["symbol"] for item in self.universe], now, max_age)
        market = market_from_groups(nifty, fno, **thresholds)
        market["taken_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        last_write = getattr(self, "last_breadth_write", None)
        if market.get("complete") and (last_write is None or (now - last_write).total_seconds() >= 60):
            self.last_breadth_write = now
            self.store.save_breadth({
                "trading_date": str(now.date()),
                "status": market["status"],
                "direction": market["direction"],
                "reason": market["reason"],
                "nifty50": market["nifty50"],
                "fno": market["fno"],
                "taken_at": market["taken_at"],
                "origin": "live",
            })
        return market

    def _snapshot(self, settings, market, prices, now) -> dict:
        from nse_scanner.engine import priced_rows
        max_age = float(settings.get("quote_max_age_seconds") or 30)
        fno_group = assess_quote_group(
            quotes_from_prices(self.universe, prices, self.cache["previous"], now),
            [item["symbol"] for item in self.universe], now, max_age,
        )
        rows = priced_rows(fno_group)
        oi_rows = None if not settings.get("oi_enabled") else self._oi_rows(rows, now)
        working = dict(settings)
        working["sector_method"] = "official_index"
        if self.sector_block:
            working["sector_block"] = self.sector_block
        members, member_notes = self._sector_members(rows)
        candidates, notes = choose_candidates(
            working, market, rows, self._sector_moves(working, members, prices, now), members, oi_rows,
        )
        if member_notes:
            notes["sector_mapping"] = " ".join(member_notes)
        snapshot = {"taken_at": now, "market": market, "candidates": candidates, "considered": rows, "notes": notes}
        write_json("pending_snapshot.json", {
            "trading_date": str(now.date()),
            "taken_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market": market,
            "candidates": candidates,
            "considered": rows,
            "notes": notes,
        })
        return snapshot

    def _sector_members(self, rows) -> tuple[dict, list[str]]:
        by_symbol = {row["symbol"]: row for row in rows}
        universe = {item["symbol"] for item in self.universe}
        members = {}
        notes = []
        for sector, symbols in self.sector_map.items():
            fno_symbols = [symbol for symbol in symbols if symbol in universe]
            if not fno_symbols:
                notes.append(f"{sector}: none of the official constituents are in the F&O equity universe.")
                continue
            missing = [symbol for symbol in fno_symbols if symbol not in by_symbol]
            if missing:
                notes.append(f"{sector}: missing a fresh price for {', '.join(missing[:8])}.")
                continue
            members[sector] = [by_symbol[symbol] for symbol in fno_symbols]
        return members, notes

    def _sector_moves(self, settings, members, prices, now) -> list[dict]:
        if not settings.get("sector_enabled"):
            return []
        if settings.get("sector_method") == "constituent_average":
            return [
                {"sector": sector, "move_pct": sum(row["change_pct"] for row in sector_rows) / len(sector_rows)}
                for sector, sector_rows in members.items()
                if sector_rows
            ]
        moves = []
        previous = self.cache.get("previous") or {}
        max_age = float(settings.get("quote_max_age_seconds") or 30)
        for item in self.indexes:
            price = prices.get(item["symbol"])
            prior = previous.get(item["symbol"])
            if not price or not prior:
                continue
            if (now - price[1]).total_seconds() > max_age:
                continue
            pct = change_pct(price[0], prior["close"])
            if pct is None:
                continue
            moves.append({
                "sector": item["sector"],
                "move_pct": pct,
                "previous_close": prior["close"],
                "current": price[0],
                "source": "Groww index " + item["symbol"],
                "as_of": price[1].strftime("%Y-%m-%d %H:%M:%S"),
            })
        return moves

    def _oi_rows(self, rows, now) -> list[dict] | None:
        try:
            frame = self._instrument_frame()
            futures = nearest_futures(frame, [row["symbol"] for row in rows], str(now.date()))
        except Exception as exc:
            self.errors.append(public_text(str(exc)))
            return None
        if len(futures) != len(rows):
            return None
        priced = {row["symbol"]: row for row in rows}
        output = []
        for symbol, future in futures.items():
            try:
                quote = self.client.get_quote(future["trading_symbol"], segment="FNO")
            except Exception as exc:
                self.errors.append(public_text(str(exc)))
                return None
            if quote.get("oi_day_change") is None:
                return None
            output.append({
                **priced[symbol],
                "oi_change": float(quote["oi_day_change"]),
                "open_interest": quote.get("open_interest"),
                "previous_open_interest": quote.get("previous_open_interest"),
                "future_symbol": future["trading_symbol"],
            })
        return output

    def _pending_for_lock(self) -> dict | None:
        if not self.pending:
            return None
        pending = dict(self.pending)
        if isinstance(pending.get("taken_at"), str):
            pending["taken_at"] = datetime.strptime(pending["taken_at"], "%Y-%m-%d %H:%M:%S")
        return pending

    def _store_lock(self, snapshot, message, trading_date, now) -> None:
        if snapshot:
            candidates = snapshot["candidates"]
            direction = snapshot["market"].get("direction")
            market = snapshot["market"]
            considered = snapshot.get("considered") or []
            notes = snapshot.get("notes") or {}
        else:
            candidates, direction, market, considered, notes = [], None, None, [], {}
        self.lock = {
            "trading_date": trading_date,
            "locked": True,
            "locked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "direction": direction,
            "market": market,
            "candidates": candidates,
            "considered": considered,
            "notes": notes,
            "reason": message,
        }
        write_json("selection_lock.json", self.lock)
        self.store.save_candidates({**self.lock, "origin": "live"})
        self.logger.info(message)

    def _emit(self, now, settings) -> list[str]:
        notes = []
        trading_date = str(now.date())
        for candidate in self.lock.get("candidates") or []:
            signals, note = signals_for_candidate(
                self.cache, candidate, self.lock["direction"], now.date(), now, settings, self.known_ids,
            )
            if note:
                notes.append(note)
            for signal in signals:
                ok, message = self.store.save_signal(signal["id"], signal)
                if not ok:
                    self.errors.append(message)
                    return notes + ["Signal storage failed, so no further signal was emitted."]
                self.known_ids.add(signal["id"])
                text = (
                    f"{signal['symbol']} {signal['direction']} {signal['setup']} "
                    f"at {signal['candle_time']}. Close {signal['close']}. Sources: {', '.join(signal['sources'])}."
                )
                notes.extend(notify_all(settings, text))
                self.logger.info("Live signal stored: %s", text)
        return notes

    def _report(self, now, settings) -> None:
        trading_date = str(now.date())
        if self.last_report_date == trading_date:
            return
        ok, signals = self.store.query_signals(trading_date, 500, None) if self.store else (False, [])
        live_signals = [row for row in signals if isinstance(row, dict) and row.get("origin") == "live"] if ok else []
        report = {
            "trading_date": trading_date,
            "origin": "live",
            "market_status": None if not self.lock else (self.lock.get("market") or {}).get("status"),
            "candidate_count": len((self.lock or {}).get("candidates") or []),
            "signal_count": len(live_signals),
            "signals": live_signals,
            "errors": self.errors[-20:],
            "note": "Signals only. This report does not place orders and does not calculate profit.",
        }
        if self.store:
            self.store.save_report(report)
        write_json("report_date.json", {"trading_date": trading_date})
        self.last_report_date = trading_date
        del settings

    def _load_known_signals(self, trading_date: str) -> None:
        if not self.store:
            return
        ok, rows = self.store.query_signals(trading_date, 500, None)
        if ok:
            self.known_ids.update(row.get("id") for row in rows if isinstance(row, dict) and row.get("id"))

    def _monitored(self) -> list[dict]:
        seen = set()
        items = []
        for item in self.nifty + self.universe + self.indexes:
            if item["symbol"] not in seen:
                seen.add(item["symbol"])
                items.append(item)
        return items

    def _load_pending(self, trading_date: str):
        data = read_json("pending_snapshot.json", None)
        if not data or data.get("trading_date") != trading_date:
            return None
        data["taken_at"] = datetime.strptime(data["taken_at"], "%Y-%m-%d %H:%M:%S")
        return data

    def _load_lock(self, trading_date: str):
        data = read_json("selection_lock.json", None)
        if not data or data.get("trading_date") != trading_date:
            return None
        return data

    def _enrich(self, row: dict, now: datetime) -> dict:
        symbol = row.get("symbol")
        completed = []
        for item in ((self.cache or {}).get("candles") or {}).get(symbol, []):
            try:
                stamp = datetime.strptime(item["ts"], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, KeyError):
                continue
            if stamp + timedelta(minutes=5) <= now:
                completed.append(item)
        previous = ((self.cache or {}).get("previous") or {}).get(symbol) or {}
        enriched = dict(row)
        if row.get("current_price_status") == "unavailable":
            enriched["ltp"] = None
        elif row.get("current_price") is not None:
            enriched["ltp"] = row["current_price"]
        if completed:
            enriched["day_high"] = max(item["high"] for item in completed)
            enriched["day_low"] = min(item["low"] for item in completed)
            enriched["volume"] = sum(item["volume"] for item in completed if item.get("volume") is not None)
        if previous:
            enriched["previous_day_high"] = previous.get("high")
            enriched["previous_day_low"] = previous.get("low")
            enriched["prev_close"] = enriched.get("prev_close") or previous.get("close")
        enriched["in_fno"] = True
        return enriched

    def _board(self, now, market, priced_candidates, signals) -> dict:
        lock = self.lock or {}
        locked = bool(lock.get("locked"))
        decision_market = lock.get("market") or market or {}
        selected = [self._enrich(row, now) for row in priced_candidates] if locked else []
        selected_symbols = {row.get("symbol") for row in selected}
        considered = [
            self._enrich(row, now)
            for row in lock.get("considered") or []
            if row.get("symbol") not in selected_symbols
        ]
        return build_board(
            decision_market,
            selected,
            considered,
            signals,
            lock.get("notes") or {},
            str(now.date()),
            now.strftime("%Y-%m-%d %H:%M:%S"),
            sector_ready=len(self.indexes) == len(SECTOR_NAMES) and not self.sector_block,
            locked=locked,
        )

    def _signal_policy(self, market) -> dict:
        lock = self.lock or {}
        locked_market = lock.get("market") or {}
        if not lock.get("direction"):
            eligibility = "No candidate list is locked, so no new signal is eligible."
        else:
            _allowed, eligibility = reversal_allows_signals(
                lock.get("direction"), market, effective_reversal_protection(load_settings())
            )
        return {
            "locked_direction": lock.get("direction") or "not locked",
            "locked_status": locked_market.get("status") or "not locked",
            "live_status": (market or {}).get("status") or "unavailable",
            "live_direction": (market or {}).get("direction") or "none",
            "eligibility": eligibility,
        }

    def publish(self, state, message, settings=None, now=None, market=None, prices=None, readiness_report=None) -> None:
        now = now or self.clock()
        settings = settings or load_settings()
        prices = prices or {}
        max_age = float(settings.get("quote_max_age_seconds") or 30)
        candidates = []
        for row in (self.lock or {}).get("candidates") or []:
            price = prices.get(row["symbol"])
            fresh = bool(price and (now - price[1]).total_seconds() <= max_age)
            candidates.append({
                **row,
                "current_price": price[0] if fresh else None,
                "current_price_status": "live" if fresh else "unavailable",
            })
        ok, signals = self.store.query_signals(str(now.date()), 200, None) if self.store else (False, [])
        live_signals = []
        if ok:
            for signal in signals:
                if not isinstance(signal, dict) or signal.get("origin") != "live":
                    continue
                price = prices.get(signal.get("symbol"))
                fresh = bool(price and (now - price[1]).total_seconds() <= max_age)
                live_signals.append({
                    **signal,
                    "current_price": price[0] if fresh else None,
                    "current_price_status": "live" if fresh else "unavailable",
                })
        previous_ready = len([item for item in self._monitored() if item["symbol"] in (self.cache or {}).get("previous", {})]) if self.cache else 0
        write_status({
            "scanner_state": state,
            "message": message,
            "trading_date": str(now.date()),
            "readiness": readiness_report,
            "market": market,
            "selection": None if not self.lock else {
                "locked": True,
                "direction": self.lock.get("direction"),
                "reason": self.lock.get("reason"),
                "market": self.lock.get("market"),
            },
            "candidates": candidates,
            "signals": live_signals,
            "board": self._board(now, market, candidates, live_signals),
            "signal_policy": self._signal_policy(market),
            "sector_block": self.sector_block,
            "orders_enabled": False,
            "lists": {
                "nifty50": (self.list_report or {}).get("nifty50") or {},
                "sectors": (self.list_report or {}).get("sectors") or {},
                "index_matches": self.index_matches,
            },
            "health": {
                "monitored": len(self._monitored()),
                "previous_close_ready": previous_ready,
                "feed": self.feed_status,
                "api_failures": getattr(self.client, "api_failures", 0),
                "last_groww_success": getattr(self.client, "last_success", None),
                "last_error": self.errors[-1] if self.errors else "",
                "errors": self.errors[-10:],
                "database": "connected" if self.store else "not connected",
            },
        })
        if self.store and self.errors:
            self.store.add_log({"at": now.isoformat(sep=" ", timespec="seconds"), "message": self.errors[-1], "state": state})


def main() -> None:
    Scanner().run()


if __name__ == "__main__":
    main()
