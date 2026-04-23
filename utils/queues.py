"""Queue helpers with bounded latest-wins behavior."""

from __future__ import annotations

from queue import Empty, Full, Queue
from typing import Any


def put_drop_oldest(queue_obj: Queue, item: Any) -> bool:
    """Put an item into a bounded queue, dropping the oldest on overflow."""
    try:
        queue_obj.put_nowait(item)
        return True
    except Full:
        try:
            queue_obj.get_nowait()
        except Empty:
            return False
        try:
            queue_obj.put_nowait(item)
            return True
        except Full:
            return False
