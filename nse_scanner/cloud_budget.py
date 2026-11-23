"""Local Firestore budget, queue, and duplicate suppression.

The scanner keeps working from local files when Firebase is down. This module
decides which cloud calls are worth making. It does not change a trading rule.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nse_scanner.paths import data_dir, runtime_dir

PACIFIC = ZoneInfo("America/Los_Angeles")
IST = ZoneInfo("Asia/Kolkata")
BREADTH_INTERVAL_SECONDS = 300
HEALTH_INTERVAL_SECONDS = 3600
MAX_BACKOFF_SECONDS = 900
DEFAULT_WRITE_TARGET = 2000

SAVED = "Saved in Firebase"
PENDING = "Pending local sync"
LOCAL_ONLY = "Local only"
FAILED = "Sync failed"


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now


def pacific_date(now: datetime) -> str:
    return _aware(now).astimezone(PACIFIC).date().isoformat()


def next_pacific_midnight(now: datetime) -> datetime:
    """The next Firestore daily reset: midnight in America/Los_Angeles."""
    pacific = _aware(now).astimezone(PACIFIC)
    return datetime.combine(pacific.date() + timedelta(days=1), clock_time.min, tzinfo=PACIFIC)


def already_recorded(message: str) -> bool:
    """A budget status is not a new Firebase error and must not start another backoff."""
    text = (message or "").lower()
    return any(marker in text for marker in (
        "daily quota is exhausted",
        "safety target",
        "permission was denied",
        "temporarily unavailable",
        "will not be retried",
        "nonessential cloud writes are paused",
    ))


def classify_firestore_failure(message: str) -> str:
    """Split a daily quota block from a short rate limit or a permission error.

    A bare HTTP 429 or RESOURCE_EXHAUSTED is a temporary backoff. Daily quota
    requires the error details to say the quota itself is exhausted.
    """
    text = (message or "").lower()
    if any(phrase in text for phrase in (
        "permission denied", "permission_denied", "permission-denied",
        "missing or insufficient permissions", "unauthenticated", "forbidden", " 403", "403 ",
    )) or text.startswith("403"):
        return "permission"
    rate = any(phrase in text for phrase in (
        "rate limit", "rate_limit", "too many requests", "retry after", "retry-after", "per minute", "per second",
    ))
    if rate:
        return "transient"
    if "429" in text and "quota" not in text:
        return "transient"
    quota = any(phrase in text for phrase in ("quota exceeded", "daily quota", "quota_exceeded"))
    exhausted = "resource exhausted" in text or "resource_exhausted" in text
    if quota and not rate:
        return "quota"
    if exhausted and "quota" in text and not rate:
        return "quota"
    return "transient"


def backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    chooser = rng or random
    base = min(MAX_BACKOFF_SECONDS, 30 * (2 ** max(0, attempt - 1)))
    if base >= MAX_BACKOFF_SECONDS:
        return float(MAX_BACKOFF_SECONDS)
    return min(float(MAX_BACKOFF_SECONDS), base + chooser.uniform(0, base * 0.2))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def instrument_upload_needed(current_checksum: str, stored_checksum: str) -> bool:
    return bool(current_checksum) and current_checksum != (stored_checksum or "")


def breadth_should_persist(now: datetime, gate, fingerprint, last_gate, last_fingerprint, last_at: datetime | None) -> bool:
    current = list(fingerprint or [])
    previous = list(last_fingerprint or [])
    if current == previous:
        return False
    if list(gate or []) != list(last_gate or []):
        return True
    if last_at is None:
        return True
    return (_aware(now) - _aware(last_at)).total_seconds() >= BREADTH_INTERVAL_SECONDS


def breadth_identity(market: dict) -> tuple[tuple, tuple]:
    nifty = market.get("nifty50") or {}
    fno = market.get("fno") or {}
    gate = (market.get("status"), market.get("direction"), bool(market.get("complete")))
    fingerprint = (
        market.get("status"),
        market.get("direction"),
        bool(market.get("complete")),
        round(float(nifty.get("adv_pct") or 0), 1),
        round(float(fno.get("adv_pct") or 0), 1),
        market.get("reason"),
    )
    return gate, fingerprint


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, default=str)
    temporary = path.with_suffix(path.suffix + ".tmp")
    for _ in range(5):
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(0.05)
        except Exception:
            return


def _load(path: Path, fallback):
    try:
        if not path.exists():
            return fallback
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _budget_path() -> Path:
    return runtime_dir() / "firestore_budget.json"


def _queue_path() -> Path:
    return data_dir() / "cloud_queue.json"


def _history_path() -> Path:
    return data_dir() / "signal_history.json"


def _report_path() -> Path:
    return data_dir() / "report_history.json"


def firebase_configured() -> bool:
    try:
        from nse_scanner.secrets import secret_status
        return bool(secret_status().get("firebase_service_account"))
    except Exception:
        return False


class FirestoreBudget:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.SystemRandom()
        self.day = ""
        self.reads = 0
        self.writes = 0
        self.deletes = 0
        self.failed = 0
        self.skipped = 0
        self.state = "open"
        self.attempt = 0
        self.next_attempt = ""
        self.quota_reset = ""
        self.permission_detail = ""
        self.last_backoff_seconds = 0.0
        self.logged_errors: list[str] = []
        self.instrument_checksum = ""
        self.public_config_checksum = ""
        self.last_health_at = ""
        self.last_breadth_gate: list = []
        self.last_breadth_fingerprint: list = []
        self.last_breadth_at = ""
        self.write_target = DEFAULT_WRITE_TARGET
        self.recovery_pending = False

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "reads": self.reads,
            "writes": self.writes,
            "deletes": self.deletes,
            "failed": self.failed,
            "skipped": self.skipped,
            "state": self.state,
            "attempt": self.attempt,
            "next_attempt": self.next_attempt,
            "quota_reset": self.quota_reset,
            "permission_detail": self.permission_detail,
            "last_backoff_seconds": self.last_backoff_seconds,
            "logged_errors": self.logged_errors[-500:],
            "instrument_checksum": self.instrument_checksum,
            "public_config_checksum": self.public_config_checksum,
            "last_health_at": self.last_health_at,
            "last_breadth_gate": self.last_breadth_gate,
            "last_breadth_fingerprint": self.last_breadth_fingerprint,
            "last_breadth_at": self.last_breadth_at,
            "write_target": self.write_target,
            "recovery_pending": self.recovery_pending,
        }

    @classmethod
    def from_dict(cls, raw: dict, rng: random.Random | None = None) -> "FirestoreBudget":
        budget = cls(rng=rng)
        for key, value in (raw or {}).items():
            if hasattr(budget, key) and key != "rng":
                setattr(budget, key, value)
        budget.logged_errors = list(budget.logged_errors or [])
        budget.last_breadth_gate = list(budget.last_breadth_gate or [])
        budget.last_breadth_fingerprint = list(budget.last_breadth_fingerprint or [])
        return budget

    def save(self) -> None:
        try:
            _dump(_budget_path(), self.to_dict())
        except Exception:
            return

    def _apply_target(self) -> None:
        try:
            from nse_scanner.settings import load_settings
            configured = int(load_settings().get("firestore_write_target") or DEFAULT_WRITE_TARGET)
        except Exception:
            configured = DEFAULT_WRITE_TARGET
        self.write_target = max(1, configured)

    def _roll(self, now: datetime) -> None:
        day = pacific_date(now)
        if self.day == day:
            return
        self.day = day
        self.reads = 0
        self.writes = 0
        self.deletes = 0
        self.failed = 0
        self.skipped = 0
        self.logged_errors = []
        self.attempt = 0
        if self.state in ("quota", "backoff"):
            self.state = "open"
            self.next_attempt = ""
            self.quota_reset = ""
        self.save()

    def allow(self, now: datetime, essential: bool = True) -> tuple[bool, str]:
        self._roll(now)
        self._apply_target()
        current = _aware(now)
        if self.state == "quota":
            reset = _parse(self.quota_reset)
            if reset is not None and current >= reset:
                self.state = "open"
                self.attempt = 0
                self.quota_reset = ""
            else:
                return False, self.status_line(now)
        if self.state == "permission":
            return False, self.status_line(now)
        if self.state == "backoff":
            retry = _parse(self.next_attempt)
            if retry is not None and current < retry:
                return False, self.status_line(now)
            self.state = "open"
            self.next_attempt = ""
        if not essential and self.writes >= self.write_target:
            return False, (
                f"Firestore write estimate is at the safety target of {self.write_target}. "
                "Nonessential cloud writes are paused. This is an application estimate, not an official Firebase quota."
            )
        return True, ""

    def note_read(self, count: int = 1) -> None:
        self.reads += max(1, int(count))

    def note_write(self, count: int = 1) -> None:
        self.writes += max(1, int(count))

    def note_delete(self, count: int = 1) -> None:
        self.deletes += max(1, int(count))

    def note_skip(self) -> None:
        self.skipped += 1

    def note_success(self) -> None:
        if self.state != "open":
            self.recovery_pending = True
        self.state = "open"
        self.attempt = 0
        self.next_attempt = ""
        self.quota_reset = ""
        self.permission_detail = ""

    def consume_recovery(self) -> bool:
        if not self.recovery_pending:
            return False
        self.recovery_pending = False
        self.save()
        return True

    def observe_failure(self, message: str, now: datetime) -> str:
        self._roll(now)
        self.failed += 1
        kind = classify_firestore_failure(message)
        if kind == "quota":
            reset = next_pacific_midnight(now)
            self.state = "quota"
            self.quota_reset = reset.isoformat(timespec="seconds")
            self.attempt = 0
            self.next_attempt = ""
        elif kind == "permission":
            self.state = "permission"
            self.permission_detail = (message or "Firestore rejected the credentials.")[:300]
            self.next_attempt = ""
        else:
            self.attempt += 1
            delay = backoff_seconds(self.attempt, self.rng)
            self.last_backoff_seconds = delay
            self.state = "backoff"
            self.next_attempt = (_aware(now) + timedelta(seconds=delay)).isoformat(timespec="seconds")
        self.save()
        return kind

    def seconds_until_retry(self, now: datetime) -> float:
        retry = _parse(self.next_attempt)
        if retry is None:
            return 0.0
        return max(0.0, (retry - _aware(now)).total_seconds())

    def should_persist_error(self, category: str, message: str) -> bool:
        key = f"{category}|{message}"
        if key in self.logged_errors:
            return False
        self.logged_errors.append(key)
        self.logged_errors = self.logged_errors[-500:]
        self.save()
        return True

    def health_due(self, now: datetime) -> bool:
        last = _parse(self.last_health_at)
        if last is None:
            return True
        return (_aware(now) - last).total_seconds() >= HEALTH_INTERVAL_SECONDS

    def mark_health(self, now: datetime) -> None:
        self.last_health_at = _aware(now).isoformat(timespec="seconds")
        self.save()

    def status_line(self, now: datetime | None = None) -> str:
        moment = now or datetime.now(IST)
        pending = pending_count()
        waiting = f" {pending} important event(s) are waiting in the local queue." if pending else ""
        if self.state == "quota":
            reset = _parse(self.quota_reset)
            when = _format_pair(reset) if reset else "the next Pacific midnight"
            return (
                "Firestore daily quota is exhausted. Cloud sync resumes at "
                f"{when}. The local scan continues.{waiting}"
            )
        if self.state == "permission":
            detail = self.permission_detail or "Firestore rejected the credentials."
            return (
                f"Firestore permission was denied: {detail} "
                "Scanning continues locally. This permission error will not be retried until the scanner restarts."
            )
        if self.state == "backoff":
            retry = _parse(self.next_attempt)
            when = _format_pair(retry) if retry else "the backoff interval"
            return f"Firestore is temporarily unavailable. Next retry at {when}. The local scan continues.{waiting}"
        if self.writes >= self.write_target:
            return (
                f"Firestore write estimate is at the safety target of {self.write_target}. "
                "Nonessential cloud writes are paused. This is an application estimate, not an official Firebase quota."
                f"{waiting}"
            )
        return (
            f"Firestore is available. Estimated writes today: {self.writes}. "
            f"Safety target {self.write_target}. This is an application estimate, not an official Firebase quota.{waiting}"
        )

    def public_view(self, now: datetime | None = None) -> dict:
        self._apply_target()
        moment = now or datetime.now(IST)
        return {
            "state": self.state,
            "status": self.status_line(moment),
            "reset_at": self.quota_reset,
            "retry_at": self.next_attempt,
            "reads": self.reads,
            "writes": self.writes,
            "deletes": self.deletes,
            "failed": self.failed,
            "skipped": self.skipped,
            "pending": pending_count(),
            "write_target": self.write_target,
            "estimated_daily_writes": self.writes,
            "day": self.day or pacific_date(moment),
        }


def load_budget() -> FirestoreBudget:
    raw = _load(_budget_path(), {})
    if not isinstance(raw, dict):
        raw = {}
    return FirestoreBudget.from_dict(raw)


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def _format_pair(moment: datetime) -> str:
    pacific = moment.astimezone(PACIFIC)
    india = moment.astimezone(IST)
    return (
        f"{india.strftime('%d %b %Y %H:%M')} IST "
        f"({pacific.strftime('%d %b %Y %H:%M')} America/Los_Angeles)"
    )


def cached_history_rows() -> list[dict]:
    rows = _load(_history_path(), [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def remember_history(signal: dict) -> None:
    try:
        rows = [row for row in cached_history_rows() if row.get("id") != signal.get("id")]
        rows.append(dict(signal))
        _dump(_history_path(), rows[-2000:])
    except Exception:
        return


def cached_reports() -> list[dict]:
    rows = _load(_report_path(), [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def remember_report(report: dict) -> None:
    try:
        rows = [row for row in cached_reports() if row.get("trading_date") != report.get("trading_date")]
        rows.append(dict(report))
        _dump(_report_path(), rows[-60:])
    except Exception:
        return


def _load_queue() -> list[dict]:
    raw = _load(_queue_path(), {"items": []})
    items = raw.get("items") if isinstance(raw, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _save_queue(items: list[dict]) -> None:
    _dump(_queue_path(), {"items": items})


def queue_state(document_id: str) -> str:
    for item in _load_queue():
        if item.get("id") == document_id:
            return str(item.get("state") or "")
    return ""


def pending_count() -> int:
    return sum(1 for item in _load_queue() if item.get("state") == "pending")


def enqueue(kind: str, document_id: str, document: dict, state: str = "pending") -> str:
    items = _load_queue()
    for item in items:
        if item.get("id") == document_id:
            if item.get("state") == "saved":
                return "saved"
            item["kind"] = kind
            item["document"] = document
            item["state"] = state
            item["updated_at"] = datetime.now(IST).isoformat(timespec="seconds")
            _save_queue(items)
            return state
    items.append({
        "id": document_id,
        "kind": kind,
        "document": document,
        "state": state,
        "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
    })
    _save_queue(items)
    return state


def _write_event(store, kind: str, document_id: str, document: dict) -> tuple[bool, str]:
    if kind == "signal":
        return store.save_signal(document_id, document)
    if kind == "report":
        return store.save_report(document)
    if kind == "candidates":
        return store.save_candidates(document)
    return False, "This cloud event is not stored."


def deliver_cloud_event(store, budget: FirestoreBudget, kind: str, document_id: str, document: dict,
                        now: datetime, configured: bool) -> str:
    existing = queue_state(document_id)
    if existing == "saved":
        budget.note_skip()
        budget.save()
        return SAVED
    if existing == "failed":
        return FAILED
    if store is not None and budget.allow(now)[0]:
        try:
            ok, message = _write_event(store, kind, document_id, document)
        except Exception as exc:
            ok, message = False, str(exc)
        if ok:
            enqueue(kind, document_id, document, "saved")
            if kind == "signal":
                remember_history({**document, "sync_state": SAVED})
            return SAVED
        if budget.state == "open" and not already_recorded(message):
            budget.observe_failure(message, now)
        label = FAILED if budget.state == "permission" else PENDING
        enqueue(kind, document_id, document, "failed" if budget.state == "permission" else "pending")
        if kind == "signal":
            remember_history({**document, "sync_state": label})
        return label
    if configured:
        enqueue(kind, document_id, document, "pending")
        if kind == "signal":
            remember_history({**document, "sync_state": PENDING})
        return PENDING
    if kind == "signal":
        remember_history({**document, "sync_state": LOCAL_ONLY})
    return LOCAL_ONLY


def flush_queue(store, budget: FirestoreBudget, now: datetime) -> int:
    if store is None or not budget.allow(now)[0]:
        return 0
    items = _load_queue()
    uploaded = 0
    changed = False
    for item in items:
        if item.get("state") != "pending":
            continue
        if not budget.allow(now)[0]:
            break
        try:
            ok, message = _write_event(store, item.get("kind"), item.get("id"), item.get("document") or {})
        except Exception as exc:
            ok, message = False, str(exc)
        changed = True
        if ok:
            item["state"] = "saved"
            uploaded += 1
            if item.get("kind") == "signal":
                remember_history({**(item.get("document") or {}), "sync_state": SAVED})
            continue
        if budget.state == "open" and not already_recorded(message):
            budget.observe_failure(message, now)
        if budget.state == "permission":
            item["state"] = "failed"
            if item.get("kind") == "signal":
                remember_history({**(item.get("document") or {}), "sync_state": FAILED})
        break
    if changed:
        try:
            _save_queue(items)
        except Exception:
            return uploaded
    return uploaded


def store_new_signal(store, budget: FirestoreBudget, known_ids: set, signal: dict, now: datetime, configured: bool) -> str:
    signal_id = signal.get("id")
    if not signal_id or signal_id in known_ids:
        budget.note_skip()
        budget.save()
        return "duplicate"
    known_ids.add(signal_id)
    return deliver_cloud_event(store, budget, "signal", signal_id, signal, now, configured)


def sync_instrument_metadata(store, budget: FirestoreBudget, checksum: str, count: int, source: str, now: datetime) -> str:
    if not instrument_upload_needed(checksum, budget.instrument_checksum):
        budget.note_skip()
        budget.save()
        return "unchanged"
    if store is None or not budget.allow(now, essential=False)[0]:
        return "deferred"
    document = {
        "checksum": checksum,
        "source": source,
        "symbol_count": count,
        "updated_at": _aware(now).isoformat(timespec="seconds"),
    }
    try:
        ok, message = store.save_instrument_metadata(document)
    except Exception as exc:
        ok, message = False, str(exc)
    if ok:
        budget.instrument_checksum = checksum
        budget.save()
        return "updated"
    if budget.state == "open" and not already_recorded(message):
        budget.observe_failure(message, now)
    return "failed"


def persist_breadth(store, budget: FirestoreBudget, market: dict, now: datetime, origin: str) -> str:
    gate, fingerprint = breadth_identity(market)
    last_at = _parse(budget.last_breadth_at)
    if not breadth_should_persist(now, gate, fingerprint, budget.last_breadth_gate, budget.last_breadth_fingerprint, last_at):
        budget.note_skip()
        budget.save()
        return "skipped"
    if store is None or not budget.allow(now, essential=False)[0]:
        return "deferred"
    document_id = f"{_aware(now).astimezone(IST).strftime('%Y-%m-%d_%H%M%S')}"
    document = {
        "id": document_id,
        "trading_date": _aware(now).astimezone(IST).date().isoformat(),
        "status": market.get("status"),
        "direction": market.get("direction"),
        "reason": market.get("reason"),
        "complete": bool(market.get("complete")),
        "nifty50": market.get("nifty50") or {},
        "fno": market.get("fno") or {},
        "taken_at": market.get("taken_at"),
        "origin": origin,
    }
    try:
        ok, message = store.save_breadth(document)
    except Exception as exc:
        ok, message = False, str(exc)
    if ok:
        budget.last_breadth_gate = list(gate)
        budget.last_breadth_fingerprint = list(fingerprint)
        budget.last_breadth_at = _aware(now).isoformat(timespec="seconds")
        budget.save()
        return "saved"
    if budget.state == "open" and not already_recorded(message):
        budget.observe_failure(message, now)
    return "failed"


def load_signal_ids_once(store, known_ids: set, state: dict, trading_date: str, now: datetime, budget: FirestoreBudget) -> None:
    if state.get("loaded"):
        return
    state["loaded"] = True
    for row in cached_history_rows():
        if row.get("id") and row.get("trading_date") in (None, "", trading_date):
            known_ids.add(row["id"])
    if store is None or not budget.allow(now)[0]:
        return
    try:
        ok, rows = store.query_signals(trading_date, 500, None)
    except Exception as exc:
        budget.observe_failure(str(exc), now)
        return
    if not ok:
        message = ""
        if rows and isinstance(rows[0], dict):
            message = rows[0].get("error") or ""
        if message:
            budget.observe_failure(message, now)
        return
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            known_ids.add(row["id"])
            remember_history({**row, "sync_state": row.get("sync_state") or SAVED})
