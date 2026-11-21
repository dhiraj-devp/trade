"""Atomic runtime status shared by the worker and the dashboard."""

from __future__ import annotations

import json
from datetime import datetime

from nse_scanner.paths import runtime_dir
from nse_scanner.redact import redact


def _write(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(redact(payload), default=_json_default), encoding="utf-8")
    temporary.replace(path)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    raise TypeError(f"Cannot store {type(value)}")


def status_path():
    return runtime_dir() / "status.json"


def write_status(payload: dict) -> None:
    payload = dict(payload)
    payload["updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    _write(status_path(), payload)


def read_status() -> dict:
    path = status_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(name: str, payload) -> None:
    _write(runtime_dir() / name, payload)


def read_json(name: str, default):
    path = runtime_dir() / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
