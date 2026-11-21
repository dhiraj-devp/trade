"""Small request pacing used by the Groww client."""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, per_second: float, per_minute: float, sleep=time.sleep, clock=time.monotonic):
        self.per_second = max(float(per_second), 0.1)
        self.per_minute = max(float(per_minute), 1.0)
        self.sleep = sleep
        self.clock = clock
        self.recent = deque()

    def wait(self) -> None:
        while True:
            now = self.clock()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            last_second = sum(1 for stamp in self.recent if now - stamp <= 1)
            if last_second < self.per_second and len(self.recent) < self.per_minute:
                self.recent.append(now)
                return
            self.sleep(0.05)
            now = self.clock()
