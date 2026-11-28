"""进程内轻量指标（配合 /metrics 暴露）。"""
from __future__ import annotations

import time
from collections import Counter
from threading import Lock


class Metrics:
    def __init__(self) -> None:
        self._started = time.time()
        self._counters: Counter[str] = Counter()
        self._lock = Lock()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            data: dict[str, float] = {"uptime_s": round(time.time() - self._started, 1)}
            data.update(dict(self._counters))
            return data


METRICS = Metrics()
