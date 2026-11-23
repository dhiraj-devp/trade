"""Start the background scanner and the dashboard together.

Closing this window stops both. A second copy does not start another scanner.
"""

from __future__ import annotations

import socket
import subprocess
import sys

from nse_scanner.control import start_worker, stop_worker, worker_running


def _dashboard_up() -> bool:
    probe = socket.socket()
    probe.settimeout(0.3)
    try:
        return probe.connect_ex(("127.0.0.1", 8501)) == 0
    finally:
        probe.close()


def main() -> int:
    started = False
    if not worker_running():
        ok, message = start_worker()
        print(message)
        if not ok and "already running" not in message.lower():
            return 1
        started = ok
    else:
        print("The scanner is already running. This window will not start a second one.")
    if _dashboard_up():
        print("The dashboard is already open at http://localhost:8501")
        return 0
    dashboard = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true"],
    )
    try:
        return dashboard.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        dashboard.terminate()
        if started or worker_running():
            _ok, message = stop_worker()
            print(message)


if __name__ == "__main__":
    raise SystemExit(main())
