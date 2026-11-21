import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_scanner_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("NSE_FNO_SCANNER_HOME", str(home))
    monkeypatch.setenv("NSE_FNO_RUNTIME_DIR", str(runtime))
    return home


@pytest.fixture
def project_root():
    return Path(__file__).resolve().parents[1]
