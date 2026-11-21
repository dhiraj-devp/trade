"""Start the background scanner and the dashboard together.

Closing this window stops both. A second copy does not start another scanner.
"""

from __future__ import annotations

import subprocess
import sys

from nse_scanner.control import start_worker, stop_worker, worker_running


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
