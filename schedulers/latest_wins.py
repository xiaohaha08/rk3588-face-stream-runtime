"""Latest-wins scheduler primitives."""

from __future__ import annotations

import threading
from typing import Generic, Optional, Sequence, TypeVar


T = TypeVar("T")


class LatestFrameSlot(Generic[T]):
    """Keep only the latest un-dispatched item for a source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: Optional[T] = None

    def put_latest(self, item: T) -> Optional[T]:
        with self._lock:
            previous = self._item
            self._item = item
            return previous

    def pop_latest(self) -> Optional[T]:
        with self._lock:
            item = self._item
            self._item = None
            return item

    def empty(self) -> bool:
        with self._lock:
            return self._item is None

    def size(self) -> int:
        with self._lock:
            return 0 if self._item is None else 1


class RoundRobinWorkerSelector:
    """Pick the next idle worker fairly without creating a queue."""

    def __init__(self) -> None:
        self._next_index = 0
        self._lock = threading.Lock()

    def pick_idle(self, workers: Sequence[object]) -> object | None:
        if not workers:
            return None
        with self._lock:
            start = self._next_index
            for offset in range(len(workers)):
                index = (start + offset) % len(workers)
                worker = workers[index]
                is_idle = getattr(worker, "is_idle", None)
                if callable(is_idle) and is_idle():
                    self._next_index = (index + 1) % len(workers)
                    return worker
        return None
