"""Firestore quota guards. These tests do not call Firebase."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nse_scanner.cloud_budget import (
    PENDING,
    SAVED,
    FirestoreBudget,
    breadth_should_persist,
    cached_history_rows,
    classify_firestore_failure,
    deliver_cloud_event,
    file_sha256,
    flush_queue,
    load_signal_ids_once,
    next_pacific_midnight,
    pending_count,
    persist_breadth,
    store_new_signal,
    sync_instrument_metadata,
)
from nse_scanner.readiness import scan_is_blocked

PACIFIC = ZoneInfo("America/Los_Angeles")
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=IST)


class FakeCloud:
    def __init__(self):
        self.metadata = []
        self.breadth = []
        self.signals = []
        self.queries = 0

    def save_instrument_metadata(self, document):
        assert "checksum" in document and "source" in document
        assert "symbol_count" in document and "updated_at" in document
        assert "raw" not in document and "symbols" not in document
        self.metadata.append(document)
        return True, "ok"

    def save_breadth(self, document):
        self.breadth.append(document)
        return True, "ok"

    def save_signal(self, document_id, document):
        self.signals.append(document_id)
        return True, "ok"

    def save_report(self, document):
        return True, "ok"

    def save_candidates(self, document):
        return True, "ok"

    def query_signals(self, trading_date, limit, after_id):
        self.queries += 1
        return True, [{"id": "known-1", "trading_date": trading_date, "origin": "live"}]


def _market(status="BULLISH", direction="LONG", adv=60.0, reason="aligned"):
    return {
        "status": status,
        "direction": direction,
        "complete": True,
        "reason": reason,
        "nifty50": {"adv_pct": adv},
        "fno": {"adv_pct": adv},
        "taken_at": "2026-11-23 10:00:00",
    }


def test_unchanged_instrument_checksum_makes_no_firestore_write(tmp_path):
    path = tmp_path / "instruments.csv"
    path.write_text("symbol\nAAA\n", encoding="utf-8")
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    digest = file_sha256(path)
    assert sync_instrument_metadata(store, budget, digest, 1, "Groww instrument master file", NOW) == "updated"
    assert sync_instrument_metadata(store, budget, digest, 1, "Groww instrument master file", NOW) == "unchanged"
    assert len(store.metadata) == 1


def test_changed_instrument_file_writes_only_compact_metadata(tmp_path):
    path = tmp_path / "instruments.csv"
    path.write_text("symbol\nAAA\n", encoding="utf-8")
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    sync_instrument_metadata(store, budget, file_sha256(path), 1, "Groww instrument master file", NOW)
    path.write_text("symbol\nAAA\nBBB\n", encoding="utf-8")
    assert sync_instrument_metadata(store, budget, file_sha256(path), 2, "Groww instrument master file", NOW) == "updated"
    assert len(store.metadata) == 2
    assert store.metadata[-1]["symbol_count"] == 2
    assert store.metadata[-1]["checksum"] != store.metadata[0]["checksum"]


def test_identical_breadth_snapshot_is_not_written_again():
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    market = _market()
    assert persist_breadth(store, budget, market, NOW, "live") == "saved"
    assert persist_breadth(store, budget, market, NOW + timedelta(minutes=10), "live") == "skipped"
    assert len(store.breadth) == 1


def test_breadth_snapshot_waits_five_minutes_when_direction_is_unchanged():
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    assert persist_breadth(store, budget, _market(adv=60.0), NOW, "live") == "saved"
    assert persist_breadth(store, budget, _market(adv=61.0), NOW + timedelta(minutes=4), "live") == "skipped"
    assert persist_breadth(store, budget, _market(adv=61.0), NOW + timedelta(minutes=5), "live") == "saved"
    assert len(store.breadth) == 2
    assert store.breadth[-1]["id"] == "2026-11-23_100500"


def test_direction_change_is_persisted_immediately():
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    persist_breadth(store, budget, _market(), NOW, "live")
    changed = _market(status="BEARISH", direction="SHORT", adv=20.0, reason="both groups fell")
    assert persist_breadth(store, budget, changed, NOW + timedelta(minutes=1), "live") == "saved"
    assert len(store.breadth) == 2
    assert store.breadth[-1]["direction"] == "SHORT"


def test_startup_signal_history_is_loaded_once():
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    known = set()
    state = {"loaded": False}
    load_signal_ids_once(store, known, state, "2026-11-23", NOW, budget)
    load_signal_ids_once(store, known, state, "2026-11-23", NOW + timedelta(seconds=5), budget)
    assert store.queries == 1
    assert "known-1" in known


def test_repeated_signal_ids_do_not_create_duplicate_writes():
    store = FakeCloud()
    budget = FirestoreBudget(rng=random.Random(1))
    known = set()
    signal = {"id": "2026-11-23_AAA_SETUP1", "origin": "live", "trading_date": "2026-11-23", "symbol": "AAA"}
    assert store_new_signal(store, budget, known, signal, NOW, True) == SAVED
    assert store_new_signal(store, budget, known, signal, NOW + timedelta(seconds=5), True) == "duplicate"
    assert store.signals == ["2026-11-23_AAA_SETUP1"]


def test_repeated_errors_do_not_create_repeated_cloud_logs():
    budget = FirestoreBudget(rng=random.Random(1))
    logs = []
    for _ in range(5):
        if budget.should_persist_error("scanner", "same failure"):
            logs.append("same failure")
    assert logs == ["same failure"]
    assert budget.should_persist_error("scanner", "a different failure") is True


def test_quota_exhaustion_activates_the_circuit_breaker_until_pacific_midnight():
    budget = FirestoreBudget(rng=random.Random(1))
    now = datetime(2026, 1, 15, 10, 0, tzinfo=PACIFIC)
    assert classify_firestore_failure("Timeout of 60.0s exceeded, last exception: 429 Quota exceeded.") == "quota"
    budget.observe_failure("429 Quota exceeded.", now)
    assert budget.state == "quota"
    assert budget.allow(now + timedelta(minutes=10))[0] is False
    store = FakeCloud()
    state = deliver_cloud_event(store, budget, "signal", "sig-1", {"id": "sig-1"}, now, True)
    assert state == PENDING
    assert store.signals == []
    assert budget.allow(next_pacific_midnight(now))[0] is True


def test_daily_reset_handles_pacific_daylight_saving_time():
    winter = datetime(2026, 1, 15, 10, 0, tzinfo=PACIFIC)
    winter_reset = next_pacific_midnight(winter)
    assert winter_reset.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M") == "2026-01-16 00:00"
    assert winter_reset.utcoffset() == timedelta(hours=-8)

    summer = datetime(2026, 7, 15, 10, 0, tzinfo=PACIFIC)
    summer_reset = next_pacific_midnight(summer)
    assert summer_reset.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M") == "2026-07-16 00:00"
    assert summer_reset.utcoffset() == timedelta(hours=-7)

    fallback = datetime(2026, 11, 1, 1, 30, tzinfo=PACIFIC)
    fallback_reset = next_pacific_midnight(fallback).astimezone(PACIFIC)
    assert fallback_reset.strftime("%Y-%m-%d %H:%M") == "2026-11-02 00:00"
    assert fallback_reset.utcoffset() == timedelta(hours=-8)


def test_temporary_network_errors_use_backoff_and_a_plain_429_is_not_daily_quota():
    assert classify_firestore_failure("429 Too Many Requests") == "transient"
    assert classify_firestore_failure("RESOURCE_EXHAUSTED: rate limit exceeded") == "transient"
    budget = FirestoreBudget(rng=random.Random(1))
    now = datetime(2026, 3, 8, 10, 0, tzinfo=IST)
    budget.observe_failure("429 Too Many Requests", now)
    assert budget.state == "backoff"
    assert budget.quota_reset == ""
    first = budget.last_backoff_seconds
    assert 30 <= first <= 900
    assert budget.allow(now + timedelta(seconds=1))[0] is False
    later = now + timedelta(seconds=first)
    budget.observe_failure("connection reset by peer", later)
    assert budget.last_backoff_seconds > first
    assert budget.last_backoff_seconds <= 900


def test_permission_errors_do_not_retry_continuously():
    budget = FirestoreBudget(rng=random.Random(1))
    now = datetime(2026, 11, 23, 12, 0, tzinfo=IST)
    budget.observe_failure("403 permission denied: Missing or insufficient permissions.", now)
    assert budget.state == "permission"
    assert budget.allow(now + timedelta(minutes=1))[0] is False
    assert budget.allow(now + timedelta(days=2))[0] is False
    assert budget.attempt == 0
    assert "permission" in budget.status_line(now).lower()


def test_local_scanner_continues_when_firestore_is_unavailable():
    report = {"checks": [
        {"name": "Groww credentials", "passed": True},
        {"name": "Firebase credentials", "passed": False},
        {"name": "Firebase connection test", "passed": False},
    ]}
    assert scan_is_blocked(report) is False
    blocked = {"checks": [{"name": "Groww credentials", "passed": False}, {"name": "Firebase credentials", "passed": False}]}
    assert scan_is_blocked(blocked) is True
    budget = FirestoreBudget(rng=random.Random(1))
    budget.observe_failure("429 Quota exceeded.", NOW)
    assert deliver_cloud_event(FakeCloud(), budget, "signal", "local-1", {"id": "local-1"}, NOW, True) == PENDING
    assert persist_breadth(None, budget, _market(), NOW, "live") == "deferred"
    assert pending_count() == 1


def test_queued_important_events_sync_without_duplicates_or_breadth_backfill():
    budget = FirestoreBudget(rng=random.Random(1))
    signal = {"id": "queued-1", "origin": "live", "trading_date": "2026-11-23", "symbol": "AAA"}
    assert deliver_cloud_event(None, budget, "signal", signal["id"], signal, NOW, True) == PENDING
    before = pending_count()
    assert persist_breadth(None, budget, _market(adv=62.0), NOW, "live") == "deferred"
    assert pending_count() == before
    store = FakeCloud()
    assert flush_queue(store, budget, NOW) == 1
    assert flush_queue(store, budget, NOW + timedelta(seconds=5)) == 0
    assert store.signals == ["queued-1"]
    assert deliver_cloud_event(store, budget, "signal", signal["id"], signal, NOW, True) == SAVED
    assert store.signals == ["queued-1"]


def test_history_screen_does_not_query_firestore_on_rerender():
    root = Path(__file__).resolve().parents[1]
    source = (root / "nse_scanner" / "ui.py").read_text(encoding="utf-8")
    history = source.split("def page_history", 1)[1].split("def page_reports", 1)[0]
    reports = source.split("def page_reports", 1)[1].split("def page_api", 1)[0]
    assert "query_signals" not in history
    assert "query_reports" not in reports
    assert "FirestoreStore" not in history
    assert "FirestoreStore" not in reports
    worker = (root / "nse_scanner" / "worker.py").read_text(encoding="utf-8")
    assert "upsert_instruments" not in worker
    assert ".ping(" not in worker
    assert cached_history_rows() == [] or isinstance(cached_history_rows(), list)


def test_breadth_interval_helper_rejects_an_identical_fingerprint():
    gate = ("BULLISH", "LONG", True)
    fingerprint = ("BULLISH", "LONG", True, 60.0, 60.0, "aligned")
    assert breadth_should_persist(NOW, gate, fingerprint, gate, fingerprint, NOW) is False
