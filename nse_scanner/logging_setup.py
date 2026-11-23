"""Logs that never keep credentials."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from nse_scanner.paths import runtime_dir
from nse_scanner.redact import public_text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return public_text(rendered)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("nse_scanner")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        runtime_dir() / "scanner.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream)
    logger.propagate = False
    return logger
