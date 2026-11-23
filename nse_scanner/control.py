"""Start and stop the background scanner without using Streamlit session state."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime

from nse_scanner.paths import project_root, runtime_dir
from nse_scanner.state import _write


def stop_path():
    return runtime_dir() / "STOP"


def pid_path():
    return runtime_dir() / "worker.json"


def request_stop() -> None:
    stop_path().write_text("stop\n", encoding="utf-8")


def clear_stop() -> None:
    path = stop_path()
    if path.exists():
        path.unlink()


def stop_requested() -> bool:
    return stop_path().exists()


def write_worker_record() -> None:
    _write(pid_path(), {"pid": os.getpid(), "started_at": datetime.now().isoformat(sep=" ", timespec="seconds")})


def read_worker_record() -> dict:
    path = pid_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_worker_record() -> None:
    path = pid_path()
    if path.exists():
        path.unlink()


def _command_line(pid: int) -> str:
    if os.name == "nt":
        command = [
            "powershell", "-NoProfile", "-Command",
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine",
        ]
    else:
        command = ["ps", "-p", str(int(pid)), "-o", "args="]
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return ""


def worker_running() -> bool:
    record = read_worker_record()
    pid = record.get("pid")
    if not pid:
        return False
    command = _command_line(int(pid))
    return "nse_scanner.worker" in command or "nse_scanner/worker" in command


def start_worker() -> tuple[bool, str]:
    if worker_running() and not stop_requested():
        return False, "The scanner process is already running."
    clear_stop()
    env = os.environ.copy()
    process = subprocess.Popen(
        [sys.executable, "-m", "nse_scanner.worker"],
        cwd=str(project_root()),
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return True, f"Scanner process started with pid {process.pid}."


def stop_worker() -> tuple[bool, str]:
    request_stop()
    record = read_worker_record()
    pid = record.get("pid")
    if not pid:
        return True, "Stop requested. No scanner pid is recorded."
    command = _command_line(int(pid))
    if "nse_scanner.worker" not in command and "nse_scanner/worker" not in command:
        return True, "Stop requested. The recorded pid is not the scanner, so it was not terminated."
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T"], check=False, timeout=8)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except OSError as exc:
        return False, f"Stop was requested, but the process could not be signalled: {exc}"
    return True, "Stop requested and the scanner process was signalled."
