"""Credential storage outside the repository.

Windows uses DPAPI so the file can only be decrypted by the same user.
Other systems store the file with owner-only permissions.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from nse_scanner.paths import secrets_dir
from nse_scanner.redact import redact

SECRETS_NAME = "credentials.bin"


def _restrict(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    in_buffer = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "nse_fno_scanner", None, None, None, 0x01, ctypes.byref(blob_out)
    ):
        raise OSError("Windows credential protection failed.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    in_buffer = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)
    ):
        raise OSError("Windows credential decryption failed.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def secrets_path() -> Path:
    return secrets_dir() / SECRETS_NAME


def load_secrets() -> dict:
    path = secrets_path()
    if not path.exists():
        return {}
    try:
        raw = _unprotect(path.read_bytes())
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_secrets(data: dict) -> None:
    path = secrets_path()
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    protected = _protect(payload)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(protected)
    _restrict(temporary)
    temporary.replace(path)
    _restrict(path)


def update_secrets(**changes) -> dict:
    current = load_secrets()
    for key, value in changes.items():
        if value is None:
            continue
        if value == "":
            current.pop(key, None)
        else:
            current[key] = value
    save_secrets(current)
    return current


def secret_status() -> dict:
    data = load_secrets()
    return {
        "storage": "Windows DPAPI" if os.name == "nt" else "owner-only file",
        "location": str(secrets_dir()),
        "groww_api_key": bool(data.get("groww_api_key")),
        "groww_api_secret": bool(data.get("groww_api_secret")),
        "groww_totp_secret": bool(data.get("groww_totp_secret")),
        "firebase_service_account": bool(data.get("firebase_service_account")),
        "telegram_token": bool(data.get("telegram_token")),
        "smtp_password": bool(data.get("smtp_password")),
        "preview": redact(data),
    }


def firebase_certificate() -> dict | None:
    data = load_secrets().get("firebase_service_account")
    return data if isinstance(data, dict) else None


def running_as_test() -> bool:
    return "pytest" in sys.modules
