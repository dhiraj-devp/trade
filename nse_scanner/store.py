"""Firestore persistence and an in-memory store used by tests.

Quotes are not written here. The collections hold instruments, breadth
snapshots, locked candidates, signals, logs, health, reports and public config.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nse_scanner.redact import public_text, redact


class Store(Protocol):
    def ping(self) -> tuple[bool, str]: ...
    def upsert_instruments(self, documents: list[dict]) -> tuple[bool, str]: ...
    def save_breadth(self, document: dict) -> tuple[bool, str]: ...
    def save_candidates(self, document: dict) -> tuple[bool, str]: ...
    def get_candidates(self, trading_date: str) -> tuple[bool, dict | None]: ...
    def get_monitoring_state(self, trading_date: str) -> tuple[bool, dict | None]: ...
    def save_monitoring_state(self, trading_date: str, document: dict) -> tuple[bool, str]: ...
    def signal_exists(self, document_id: str) -> bool: ...
    def save_signal(self, document_id: str, document: dict) -> tuple[bool, str]: ...
    def add_log(self, document: dict) -> tuple[bool, str]: ...
    def save_health(self, name: str, document: dict) -> tuple[bool, str]: ...
    def save_report(self, document: dict) -> tuple[bool, str]: ...
    def save_public_config(self, document: dict) -> tuple[bool, str]: ...
    def save_sectors(self, documents: list[dict]) -> tuple[bool, str]: ...
    def query_signals(self, trading_date: str | None, limit: int, after_id: str | None) -> tuple[bool, list]: ...
    def query_reports(self, limit: int) -> tuple[bool, list]: ...
    def recent_logs(self, limit: int) -> tuple[bool, list]: ...


_FAST_IO = {"retry": None, "timeout": 8.0}


def cloud_is_limited(message: str) -> bool:
    """True only for a confirmed daily quota failure, not every HTTP 429."""
    from nse_scanner.cloud_budget import classify_firestore_failure
    return classify_firestore_failure(message) == "quota"


def scoped_collection(name: str, developer_mode: bool) -> str:
    """Developer records use a dev_ prefix. Production collection names stay unchanged."""
    if developer_mode and not name.startswith("dev_"):
        return f"dev_{name}"
    return name


class MemoryStore:
    def __init__(self):
        self.developer_mode = False
        self.instruments = {}
        self.sectors = {}
        self.breadth = {}
        self.candidates = {}
        self.monitoring_state = {}
        self.signals = {}
        self.logs = []
        self.health = {}
        self.reports = {}
        self.config = {}
        self.fail = False

    def ping(self) -> tuple[bool, str]:
        if self.fail:
            return False, "Memory store is marked failed."
        self.health["connection_test"] = {"ok": True}
        return True, "In-memory store read and write succeeded."

    def upsert_instruments(self, documents: list[dict]) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        for document in documents:
            self.instruments[document["symbol"]] = redact(document)
        return True, f"Stored {len(documents)} instruments."

    def save_breadth(self, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.breadth[document["trading_date"]] = redact(document)
        return True, "Breadth snapshot stored."

    def save_candidates(self, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.candidates[document["trading_date"]] = redact(document)
        return True, "Candidates stored."

    def get_candidates(self, trading_date: str) -> tuple[bool, dict | None]:
        if self.fail:
            return False, None
        return True, self.candidates.get(trading_date)

    def get_monitoring_state(self, trading_date: str) -> tuple[bool, dict | None]:
        if self.fail:
            return False, None
        return True, self.monitoring_state.get(trading_date)

    def save_monitoring_state(self, trading_date: str, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.monitoring_state[trading_date] = redact(document)
        return True, "Monitoring state stored."

    def signal_exists(self, document_id: str) -> bool:
        return document_id in self.signals

    def save_signal(self, document_id: str, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        if document_id in self.signals:
            return True, "Signal already stored."
        stored = redact(document)
        stored["id"] = document_id
        self.signals[document_id] = stored
        return True, "Signal stored."

    def add_log(self, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.logs.append(redact(document))
        return True, "Log stored."

    def save_health(self, name: str, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.health[name] = redact(document)
        return True, "Health stored."

    def save_report(self, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.reports[document["trading_date"]] = redact(document)
        return True, "Report stored."

    def save_public_config(self, document: dict) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        self.config = redact(document)
        return True, "Public config stored."

    def save_sectors(self, documents: list[dict]) -> tuple[bool, str]:
        if self.fail:
            return False, "Store unavailable."
        for document in documents:
            self.sectors[document["sector"]] = redact(document)
        return True, "Sectors stored."

    def query_signals(self, trading_date: str | None, limit: int, after_id: str | None) -> tuple[bool, list]:
        rows = [row for row in self.signals.values() if trading_date in (None, "", row.get("trading_date"))]
        rows.sort(key=lambda row: row.get("id", ""))
        if after_id:
            rows = [row for row in rows if row.get("id", "") > after_id]
        return True, rows[:limit]

    def query_reports(self, limit: int) -> tuple[bool, list]:
        rows = sorted(self.reports.values(), key=lambda row: row.get("trading_date", ""), reverse=True)
        return True, rows[:limit]

    def recent_logs(self, limit: int) -> tuple[bool, list]:
        return True, list(self.logs[-limit:])


class FirestoreStore:
    def __init__(self, client, developer_mode: bool = False, budget=None):
        from nse_scanner.cloud_budget import load_budget

        self.client = client
        self.developer_mode = bool(developer_mode)
        self.budget = budget or load_budget()

    @classmethod
    def connect(cls, budget=None):
        import firebase_admin
        from firebase_admin import credentials, firestore

        from nse_scanner.secrets import firebase_certificate

        certificate = firebase_certificate()
        if not certificate:
            raise RuntimeError("Firebase service account is not configured.")
        try:
            app = firebase_admin.get_app("nse_fno_scanner")
        except ValueError:
            app = firebase_admin.initialize_app(credentials.Certificate(certificate), name="nse_fno_scanner")
        from nse_scanner.settings import developer_mode_enabled

        return cls(firestore.client(app), developer_mode_enabled(), budget=budget)

    def _collection(self, name: str):
        return self.client.collection(scoped_collection(name, self.developer_mode))

    def ping(self) -> tuple[bool, str]:
        """One read of the existing health document. No write and no delete."""
        return self.check_connection()

    def check_connection(self) -> tuple[bool, str]:
        allowed, message = self.budget.allow(datetime.now(), essential=True)
        if not allowed:
            return False, message
        try:
            loaded = self._collection("api_health").document("worker").get(**_FAST_IO)
            self.budget.note_read()
            self.budget.note_success()
            self.budget.save()
            if loaded.exists:
                return True, "Firestore read succeeded."
            return True, "Firestore answered. The worker health document has not been written yet."
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, text

    def preflight_roundtrip(self, document_id: str) -> tuple[bool, str]:
        """Write, read, and delete one preflight document. No other document is changed."""
        if not document_id.startswith("preflight_"):
            return False, "The preflight document id was refused."
        ref = self._collection("api_health").document(document_id)
        try:
            allowed, message = self.budget.allow(datetime.now(), essential=False)
            if not allowed:
                self.budget.note_skip()
                self.budget.save()
                return False, message
            ref.set({"purpose": "preflight", "ok": True, "tested_at": datetime.utcnow().isoformat()}, **_FAST_IO)
            self.budget.note_write()
            loaded = ref.get(**_FAST_IO)
            self.budget.note_read()
            if not loaded.exists or (loaded.to_dict() or {}).get("purpose") != "preflight":
                self.budget.save()
                return False, "The preflight document could not be read back."
            ref.delete(**_FAST_IO)
            self.budget.note_delete()
            still_there = ref.get(**_FAST_IO)
            self.budget.note_read()
            self.budget.note_success()
            self.budget.save()
            if still_there.exists:
                return False, "The preflight document could not be deleted."
            return True, f"Firestore wrote, read, and deleted api_health/{document_id}."
        except Exception as exc:
            try:
                ref.delete()
            except Exception:
                pass
            return False, public_text(str(exc))

    def upsert_instruments(self, documents: list[dict]) -> tuple[bool, str]:
        """One checksum document. The raw instrument file stays on disk."""
        import hashlib
        import json
        raw = json.dumps(documents, sort_keys=True, default=str).encode("utf-8")
        return self.save_instrument_metadata({
            "checksum": hashlib.sha256(raw).hexdigest(),
            "source": "derived instrument list",
            "symbol_count": len(documents),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })

    def save_instrument_metadata(self, document: dict) -> tuple[bool, str]:
        return self._set("instruments_meta", "current", document, essential=False)

    def save_breadth(self, document: dict) -> tuple[bool, str]:
        document_id = str(document.get("id") or document["trading_date"])
        return self._set("market_breadth", document_id, document, essential=False)

    def save_candidates(self, document: dict) -> tuple[bool, str]:
        return self._set("daily_candidates", document["trading_date"], document, essential=True)

    def get_candidates(self, trading_date: str) -> tuple[bool, dict | None]:
        allowed, message = self.budget.allow(datetime.now(), essential=True)
        if not allowed:
            return False, None
        try:
            doc = self._collection("daily_candidates").document(trading_date).get(**_FAST_IO)
            self.budget.note_read()
            self.budget.note_success()
            self.budget.save()
            if doc.exists:
                data = doc.to_dict() or {}
                data.setdefault("id", doc.id)
                return True, data
            return True, None
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, None

    def get_monitoring_state(self, trading_date: str) -> tuple[bool, dict | None]:
        allowed, message = self.budget.allow(datetime.now(), essential=True)
        if not allowed:
            return False, None
        try:
            doc = self._collection("monitoring_state").document(trading_date).get(**_FAST_IO)
            self.budget.note_read()
            self.budget.note_success()
            self.budget.save()
            if doc.exists:
                data = doc.to_dict() or {}
                data.setdefault("id", doc.id)
                return True, data
            return True, None
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, None

    def save_monitoring_state(self, trading_date: str, document: dict) -> tuple[bool, str]:
        return self._set("monitoring_state", trading_date, document, essential=True)

    def signal_exists(self, document_id: str) -> bool:
        allowed, _message = self.budget.allow(datetime.now(), essential=True)
        if not allowed:
            return False
        try:
            exists = self._collection("scanner_signals").document(document_id).get(**_FAST_IO).exists
            self.budget.note_read()
            self.budget.save()
            return exists
        except Exception as exc:
            self.budget.observe_failure(public_text(str(exc)), datetime.now())
            return False

    def save_signal(self, document_id: str, document: dict) -> tuple[bool, str]:
        return self._set("scanner_signals", document_id, {**document, "id": document_id}, essential=True)

    def add_log(self, document: dict) -> tuple[bool, str]:
        import hashlib
        day = str(document.get("day") or datetime.now().date())
        identity = f"{document.get('category') or 'scanner'}|{document.get('message') or ''}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return self._set("scanner_logs", f"{day}_{digest}", {**document, "day": day}, essential=False)

    def save_health(self, name: str, document: dict) -> tuple[bool, str]:
        return self._set("api_health", name, document, essential=False)

    def save_report(self, document: dict) -> tuple[bool, str]:
        return self._set("daily_reports", document["trading_date"], document, essential=True)

    def save_public_config(self, document: dict) -> tuple[bool, str]:
        return self._set("application_config", "public", document, essential=False)

    def save_sectors(self, documents: list[dict]) -> tuple[bool, str]:
        allowed, message = self.budget.allow(datetime.now(), essential=False)
        if not allowed:
            self.budget.note_skip()
            self.budget.save()
            return False, message
        try:
            batch = self.client.batch()
            for document in documents:
                ref = self._collection("sectors").document(document["sector"].replace(" ", "_"))
                batch.set(ref, redact(document))
            batch.commit(**_FAST_IO)
            self.budget.note_write(max(1, len(documents)))
            self.budget.note_success()
            self.budget.save()
            return True, "Sectors stored."
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, text

    def query_signals(self, trading_date: str | None, limit: int, after_id: str | None) -> tuple[bool, list]:
        return self._query("scanner_signals", trading_date, limit, after_id, "trading_date", "id")

    def query_reports(self, limit: int) -> tuple[bool, list]:
        allowed, message = self.budget.allow(datetime.now(), essential=True)
        if not allowed:
            self.budget.note_skip()
            self.budget.save()
            return False, [{"error": message}]
        try:
            rows = [self._plain(item) for item in self._collection("daily_reports").limit(max(limit, 30)).stream(**_FAST_IO)]
            self.budget.note_read(max(1, len(rows)))
            self.budget.note_success()
            self.budget.save()
            rows.sort(key=lambda row: row.get("trading_date", ""), reverse=True)
            return True, rows[:limit]
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, [{"error": text}]

    def recent_logs(self, limit: int) -> tuple[bool, list]:
        allowed, message = self.budget.allow(datetime.now(), essential=False)
        if not allowed:
            self.budget.note_skip()
            self.budget.save()
            return False, [{"error": message}]
        try:
            rows = [self._plain(item) for item in self._collection("scanner_logs").limit(limit).stream(**_FAST_IO)]
            self.budget.note_read(max(1, len(rows)))
            self.budget.save()
            return True, rows
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, [{"error": text}]

    def _query(self, collection: str, trading_date: str | None, limit: int, after_id: str | None,
               date_field: str, sort_field: str) -> tuple[bool, list]:
        allowed, message = self.budget.allow(datetime.now(), essential=True)
        if not allowed:
            self.budget.note_skip()
            self.budget.save()
            return False, [{"error": message}]
        try:
            query = self._collection(collection)
            if trading_date:
                query = query.where(date_field, "==", trading_date)
            rows = [self._plain(item) for item in query.limit(500).stream(**_FAST_IO)]
            self.budget.note_read(max(1, len(rows)))
            self.budget.note_success()
            self.budget.save()
            rows.sort(key=lambda row: row.get(sort_field, ""))
            if after_id:
                rows = [row for row in rows if row.get(sort_field, "") > after_id]
            return True, rows[:limit]
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, [{"error": text}]

    def _set(self, collection: str, document_id: str, document: dict, essential: bool = False) -> tuple[bool, str]:
        allowed, message = self.budget.allow(datetime.now(), essential=essential)
        if not allowed:
            self.budget.note_skip()
            self.budget.save()
            return False, message
        try:
            self._collection(collection).document(document_id).set(redact(document), **_FAST_IO)
            self.budget.note_write()
            self.budget.note_success()
            self.budget.save()
            return True, f"Stored {collection}/{document_id}."
        except Exception as exc:
            text = public_text(str(exc))
            self.budget.observe_failure(text, datetime.now())
            return False, text

    @staticmethod
    def _plain(snapshot) -> dict:
        data = snapshot.to_dict() or {}
        data.setdefault("id", snapshot.id)
        return data
