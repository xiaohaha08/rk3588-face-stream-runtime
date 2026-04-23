"""Shared runtime worker thread implementation."""

from __future__ import annotations

import logging
import threading
import time
from queue import Queue
from typing import Optional

from pipelines.types import InferenceResult, WorkerStatus, WorkerTask
from services.config import WorkerConfig
from utils.queues import put_drop_oldest


LOGGER = logging.getLogger(__name__)


class WorkerThread(threading.Thread):
    """Single-task mailbox worker. Idle-only dispatch keeps queues bounded."""

    def __init__(
        self,
        worker_id: int,
        worker_config: WorkerConfig,
        processor,
        result_queue: Queue,
        source_metrics,
    ) -> None:
        super().__init__(name=f"Worker-{worker_id}", daemon=True)
        self.worker_id = int(worker_id)
        self.worker_config = worker_config
        self.processor = processor
        self.result_queue = result_queue
        self.source_metrics = source_metrics
        self._condition = threading.Condition()
        self._task: Optional[WorkerTask] = None
        self._busy = False
        self._stop_event = threading.Event()

    def submit_if_idle(self, task: WorkerTask) -> bool:
        with self._condition:
            if self._busy or self._task is not None or self._stop_event.is_set():
                return False
            self._task = task
            self._busy = True
            self._condition.notify()
            return True

    def is_idle(self) -> bool:
        with self._condition:
            return (not self._busy) and self._task is None

    def stop(self) -> None:
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()

    def _publish(self, result: InferenceResult) -> None:
        put_drop_oldest(self.result_queue, result)

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                task = None
                with self._condition:
                    while self._task is None and not self._stop_event.is_set():
                        self._condition.wait(timeout=0.1)
                    if self._stop_event.is_set():
                        break
                    task = self._task
                    self._task = None

                if task is None:
                    continue

                try:
                    worker_started = time.perf_counter()
                    result = self.processor.process(task, self.worker_id)
                    effective_started = result.started_monotonic if result.started_monotonic > 0.0 else worker_started
                    timing_ms = dict(result.timing_ms or {})
                    timing_ms.setdefault("worker_process", float(result.process_ms))
                    timing_ms["capture_to_worker_start"] = max(
                        0.0,
                        (effective_started - task.capture_monotonic) * 1000.0,
                    )
                    result.started_monotonic = effective_started
                    result.timing_ms = timing_ms
                    if result.completed_monotonic > task.deadline_monotonic:
                        result.status = WorkerStatus.EXPIRED
                        self.source_metrics.record_worker_timeout()
                    self.source_metrics.record_worker_completion(result.process_ms, timing_ms=result.timing_ms)
                except Exception as exc:
                    completed = time.perf_counter()
                    self.source_metrics.record_exception()
                    result = InferenceResult(
                        source_id=task.source_id,
                        frame_id=task.frame_id,
                        status=WorkerStatus.FAILED,
                        detections=[],
                        worker_id=self.worker_id,
                        started_monotonic=0.0,
                        completed_monotonic=completed,
                        process_ms=0.0,
                        error=str(exc),
                        timing_ms={"capture_to_worker_start": max(0.0, (completed - task.capture_monotonic) * 1000.0)},
                    )
                    LOGGER.exception("worker %s failed on frame %s", self.worker_id, task.frame_id)
                    self.source_metrics.record_worker_completion(result.process_ms, timing_ms=result.timing_ms)

                self._publish(result)
                with self._condition:
                    self._busy = False
        finally:
            close_fn = getattr(self.processor, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    LOGGER.exception("worker %s processor close failed", self.worker_id)
