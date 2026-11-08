"""재시작해도 겹치지 않는 BIGINT ID. 시각(ms) 41비트 + 워커 6비트 + 순번 16비트."""

from __future__ import annotations

import datetime as dt
import threading

EPOCH_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z


class IdGen:
    def __init__(self, worker: int = 0) -> None:
        if not 0 <= worker < 64:
            raise ValueError("worker 는 0~63")
        self.worker = worker
        self._last_ms = -1
        self._seq = 0
        self._lock = threading.Lock()

    def next(self, at: dt.datetime) -> int:
        ms = int(at.timestamp() * 1000) - EPOCH_MS
        with self._lock:
            if ms <= self._last_ms:
                ms = self._last_ms
                self._seq += 1
                if self._seq >= 1 << 16:
                    ms += 1
                    self._seq = 0
            else:
                self._seq = 0
            self._last_ms = ms
            return (ms << 22) | (self.worker << 16) | self._seq
