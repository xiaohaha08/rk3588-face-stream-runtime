"""控制面的轻量事件总线。"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from queue import Queue
from typing import Any

from utils.queues import put_drop_oldest


@dataclass(frozen=True)
class EventEnvelope:
    event_id: int
    topic: str
    source_id: str
    payload: dict[str, Any]
    timestamp_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "source_id": self.source_id,
            "payload": self.payload,
            "timestamp_ms": self.timestamp_ms,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)


class EventBus:
    """线程安全的事件总线，支持最近事件缓存和有界订阅。"""

    def __init__(self, max_events: int = 256) -> None:
        self.max_events = max(1, int(max_events))
        self._lock = threading.Lock()
        self._next_event_id = 1
        self._events: deque[EventEnvelope] = deque(maxlen=self.max_events)
        self._subscribers: dict[int, Queue] = {}
        self._next_subscriber_id = 1

    def publish(self, topic: str, source_id: str, payload: dict[str, Any]) -> EventEnvelope:
        with self._lock:
            envelope = EventEnvelope(
                event_id=self._next_event_id,
                topic=str(topic),
                source_id=str(source_id),
                payload=dict(payload),
                timestamp_ms=int(time.time() * 1000),
            )
            self._next_event_id += 1
            self._events.append(envelope)
            subscribers = list(self._subscribers.values())

        for queue_obj in subscribers:
            put_drop_oldest(queue_obj, envelope)
        return envelope

    def recent(self, limit: int = 100, topic: str | None = None) -> list[dict[str, Any]]:
        max_items = max(1, int(limit))
        with self._lock:
            items = list(self._events)
        if topic:
            items = [item for item in items if item.topic == topic]
        return [item.as_dict() for item in items[-max_items:]]

    def subscribe(self, max_queue: int = 64) -> tuple[int, Queue]:
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            queue_obj: Queue = Queue(maxsize=max(1, int(max_queue)))
            self._subscribers[subscriber_id] = queue_obj
            return subscriber_id, queue_obj

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers.pop(int(subscriber_id), None)
