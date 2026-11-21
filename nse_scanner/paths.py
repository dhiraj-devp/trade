"""Filesystem locations. Secrets stay outside the project directory."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def app_home() -> Path:
    override = os.environ.get("NSE_FNO_SCANNER_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "nse_fno_scanner"
    return Path.home() / ".config" / "nse_fno_scanner"


def secrets_dir() -> Path:
    path = app_home() / "secrets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lists_dir() -> Path:
    path = app_home() / "lists"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    override = os.environ.get("NSE_FNO_RUNTIME_DIR")
    path = Path(override) if override else project_root() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def templates_dir() -> Path:
    return project_root() / "templates"
