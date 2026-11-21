"""Remove credential material from messages and structured logs."""

from __future__ import annotations

import re

_SECRET_PATTERN = re.compile(r"(?i)(api[_ ]?key|secret|token|password|private[_ ]key)\s*[:=]\s*\S+")


def _sensitive(key: str) -> bool:
    lowered = str(key).lower()
    if lowered == "exchange_token":
        return False
    if lowered == "token" or lowered.endswith("_token"):
        return True
    markers = ("api_key", "secret", "password", "private_key", "credential", "authorization", "service_account", "totp")
    return any(marker in lowered for marker in markers)


def redact(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if _sensitive(key):
                clean[key] = "configured" if item else ""
            else:
                clean[key] = redact(item)
        return clean
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(r"\1=[redacted]", value)
    return value


def public_text(value: str) -> str:
    text = redact(value) if not isinstance(value, str) else _SECRET_PATTERN.sub(r"\1=[redacted]", value)
    lowered = text.lower()
    for marker in ("private_key", "begin private", "api_secret", "totp"):
        if marker in lowered and "redacted" not in lowered:
            return "A credential was removed from this message."
    return text
