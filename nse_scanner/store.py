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


class MemoryStore:
    def __init__(self):
        self.instruments = {}
        self.sectors = {}
        self.breadth = {}
        self.candidates = {}
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
    def __init__(self, client):
        self.client = client

    @classmethod
    def connect(cls):
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
        return cls(firestore.client(app))

    def _collection(self, name: str):
        return self.client.collection(name)

    def ping(self) -> tuple[bool, str]:
        try:
            document = self._collection("api_health").document("connection_test")
            document.set({"ok": True, "tested_at": datetime.utcnow().isoformat()})
            loaded = document.get()
            if not loaded.exists:
                return False, "Firestore write could not be read back."
            document.delete()
            return True, "Firestore read and write test succeeded."
        except Exception as exc:
            return False, public_text(str(exc))

    def preflight_roundtrip(self, document_id: str) -> tuple[bool, str]:
        """Write, read, and delete one preflight document. No other document is changed."""
        if not document_id.startswith("preflight_"):
            return False, "The preflight document id was refused."
        ref = self._collection("api_health").document(document_id)
        try:
            ref.set({"purpose": "preflight", "ok": True, "tested_at": datetime.utcnow().isoformat()})
            loaded = ref.get()
            if not loaded.exists or (loaded.to_dict() or {}).get("purpose") != "preflight":
                return False, "The preflight document could not be read back."
            ref.delete()
            if ref.get().exists:
                return False, "The preflight document could not be deleted."
            return True, f"Firestore wrote, read, and deleted api_health/{document_id}."
        except Exception as exc:
            try:
                ref.delete()
            except Exception:
                pass
            return False, public_text(str(exc))

    def upsert_instruments(self, documents: list[dict]) -> tuple[bool, str]:
        try:
            batch = self.client.batch()
            count = 0
            for document in documents:
                ref = self._collection("instruments").document(document["symbol"])
                batch.set(ref, redact(document))
                count += 1
                if count == 400:
                    batch.commit()
                    batch = self.client.batch()
                    count = 0
            if count:
                batch.commit()
            return True, f"Stored {len(documents)} instruments."
        except Exception as exc:
            return False, public_text(str(exc))

    def save_breadth(self, document: dict) -> tuple[bool, str]:
        return self._set("market_breadth", document["trading_date"], document)

    def save_candidates(self, document: dict) -> tuple[bool, str]:
        return self._set("daily_candidates", document["trading_date"], document)

    def signal_exists(self, document_id: str) -> bool:
        try:
            return self._collection("scanner_signals").document(document_id).get().exists
        except Exception:
            return False

    def save_signal(self, document_id: str, document: dict) -> tuple[bool, str]:
        try:
            ref = self._collection("scanner_signals").document(document_id)
            if ref.get().exists:
                return True, "Signal already stored."
            ref.set(redact({**document, "id": document_id}))
            return True, "Signal stored."
        except Exception as exc:
            return False, public_text(str(exc))

    def add_log(self, document: dict) -> tuple[bool, str]:
        try:
            self._collection("scanner_logs").add(redact(document))
            return True, "Log stored."
        except Exception as exc:
            return False, public_text(str(exc))

    def save_health(self, name: str, document: dict) -> tuple[bool, str]:
        return self._set("api_health", name, document)

    def save_report(self, document: dict) -> tuple[bool, str]:
        return self._set("daily_reports", document["trading_date"], document)

    def save_public_config(self, document: dict) -> tuple[bool, str]:
        return self._set("application_config", "public", document)

    def save_sectors(self, documents: list[dict]) -> tuple[bool, str]:
        try:
            batch = self.client.batch()
            for document in documents:
                ref = self._collection("sectors").document(document["sector"].replace(" ", "_"))
                batch.set(ref, redact(document))
            batch.commit()
            return True, "Sectors stored."
        except Exception as exc:
            return False, public_text(str(exc))

    def query_signals(self, trading_date: str | None, limit: int, after_id: str | None) -> tuple[bool, list]:
        try:
            query = self._collection("scanner_signals")
            if trading_date:
                query = query.where("trading_date", "==", trading_date)
            rows = [self._plain(item) for item in query.limit(500).stream()]
            rows.sort(key=lambda row: row.get("id", ""))
            if after_id:
                rows = [row for row in rows if row.get("id", "") > after_id]
            return True, rows[:limit]
        except Exception as exc:
            return False, [{"error": public_text(str(exc))}]

    def query_reports(self, limit: int) -> tuple[bool, list]:
        try:
            rows = [self._plain(item) for item in self._collection("daily_reports").limit(max(limit, 30)).stream()]
            rows.sort(key=lambda row: row.get("trading_date", ""), reverse=True)
            return True, rows[:limit]
        except Exception as exc:
            return False, [{"error": public_text(str(exc))}]

    def recent_logs(self, limit: int) -> tuple[bool, list]:
        try:
            rows = [self._plain(item) for item in self._collection("scanner_logs").limit(limit).stream()]
            return True, rows
        except Exception as exc:
            return False, [{"error": public_text(str(exc))}]

    def _set(self, collection: str, document_id: str, document: dict) -> tuple[bool, str]:
        try:
            self._collection(collection).document(document_id).set(redact(document))
            return True, f"Stored {collection}/{document_id}."
        except Exception as exc:
            return False, public_text(str(exc))

    @staticmethod
    def _plain(snapshot) -> dict:
        data = snapshot.to_dict() or {}
        data.setdefault("id", snapshot.id)
        return data
