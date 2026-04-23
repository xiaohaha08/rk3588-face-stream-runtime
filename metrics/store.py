"""Bounded per-source metrics store for stage-1 validation."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict


class SourceMetrics:
    """Thread-safe source metrics with sliding windows."""

    _TIMING_MAXLEN = 256
    _BOTTLENECK_STAGE_KEYS = (
        "capture_read",
        "capture_to_worker_start",
        "worker_process",
        "output_wait",
        "output_prepare",
        "push_queue_wait",
        "push_write",
    )

    def __init__(self, source_id: str, window_seconds: float = 5.0) -> None:
        self.source_id = source_id
        self.window_seconds = float(window_seconds)
        self._lock = threading.Lock()
        self._capture_times: Deque[float] = deque()
        self._dispatch_times: Deque[float] = deque()
        self._output_times: Deque[float] = deque()
        self._worker_ms: Deque[float] = deque(maxlen=256)
        self._e2e_ms: Deque[float] = deque(maxlen=256)
        self._stage_ms: Dict[str, Deque[float]] = {}
        self._last_stage_ms: Dict[str, float] = {}
        self._last_pipeline_timing_ms: Dict[str, float] = {}
        self._last_e2e_ms = 0.0
        self._exception_times: Deque[float] = deque()
        self._queue_depths: Dict[str, int] = {}
        self._queue_high_watermarks: Dict[str, int] = {}
        self._drop_reasons: Dict[str, int] = {}
        self.capture_count = 0
        self.dispatch_count = 0
        self.output_count = 0
        self.worker_timeout_count = 0
        self.dropped_count = 0
        self.reorder_wait_count = 0
        self.reorder_skip_count = 0
        self.recognition_submit_count = 0
        self.recognition_result_count = 0
        self.recognition_match_count = 0
        self.recognition_reject_count = 0

    def _trim_window(self, now: float) -> None:
        for bucket in (self._capture_times, self._dispatch_times, self._output_times, self._exception_times):
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()

    def _rate(self, bucket: Deque[float]) -> float:
        return len(bucket) / self.window_seconds if self.window_seconds > 0 else 0.0

    def record_capture(self, now: float | None = None) -> None:
        ts = now if now is not None else time.perf_counter()
        with self._lock:
            self.capture_count += 1
            self._capture_times.append(ts)
            self._trim_window(ts)

    def record_dispatch(self, now: float | None = None) -> None:
        ts = now if now is not None else time.perf_counter()
        with self._lock:
            self.dispatch_count += 1
            self._dispatch_times.append(ts)
            self._trim_window(ts)

    def _observe_stage_timing_locked(self, name: str, value: float) -> None:
        stage_name = str(name or "").strip()
        if not stage_name:
            return
        numeric = max(0.0, float(value))
        bucket = self._stage_ms.get(stage_name)
        if bucket is None:
            bucket = deque(maxlen=self._TIMING_MAXLEN)
            self._stage_ms[stage_name] = bucket
        bucket.append(numeric)
        self._last_stage_ms[stage_name] = numeric

    def observe_stage_timing(self, name: str, value: float) -> None:
        with self._lock:
            self._observe_stage_timing_locked(name, value)

    def observe_stage_timings(self, values: Dict[str, float] | None) -> None:
        if not values:
            return
        with self._lock:
            for name, value in values.items():
                self._observe_stage_timing_locked(name, value)

    def record_worker_completion(self, process_ms: float, timing_ms: Dict[str, float] | None = None) -> None:
        timings = dict(timing_ms or {})
        timings.setdefault("worker_process", float(process_ms))
        with self._lock:
            self._worker_ms.append(float(process_ms))
            for name, value in timings.items():
                self._observe_stage_timing_locked(name, value)

    def record_worker_timeout(self) -> None:
        with self._lock:
            self.worker_timeout_count += 1

    def record_drop(self, reason: str) -> None:
        with self._lock:
            self.dropped_count += 1
            key = str(reason or "unknown")
            self._drop_reasons[key] = self._drop_reasons.get(key, 0) + 1

    def record_output(
        self,
        capture_monotonic: float,
        now: float | None = None,
        timing_ms: Dict[str, float] | None = None,
    ) -> None:
        ts = now if now is not None else time.perf_counter()
        e2e_ms = max(0.0, (ts - capture_monotonic) * 1000.0)
        last_pipeline_timing = dict(timing_ms or {})
        last_pipeline_timing["e2e_total"] = e2e_ms
        with self._lock:
            self.output_count += 1
            self._output_times.append(ts)
            self._e2e_ms.append(e2e_ms)
            self._last_e2e_ms = e2e_ms
            self._last_pipeline_timing_ms = last_pipeline_timing
            self._last_stage_ms["e2e_total"] = e2e_ms
            self._trim_window(ts)

    def record_reorder_wait(self) -> None:
        with self._lock:
            self.reorder_wait_count += 1

    def record_reorder_skip(self) -> None:
        with self._lock:
            self.reorder_skip_count += 1

    def record_exception(self) -> None:
        ts = time.perf_counter()
        with self._lock:
            self._exception_times.append(ts)
            self._trim_window(ts)

    def record_recognition_submit(self) -> None:
        with self._lock:
            self.recognition_submit_count += 1

    def record_recognition_result(self, matched: bool) -> None:
        with self._lock:
            self.recognition_result_count += 1
            if matched:
                self.recognition_match_count += 1

    def record_recognition_reject(self) -> None:
        with self._lock:
            self.recognition_reject_count += 1

    def observe_queue(self, name: str, depth: int) -> None:
        with self._lock:
            current = max(0, int(depth))
            self._queue_depths[name] = current
            self._queue_high_watermarks[name] = max(current, self._queue_high_watermarks.get(name, 0))

    def _timing_average_locked(self, name: str) -> float:
        bucket = self._stage_ms.get(name)
        if not bucket:
            return 0.0
        return sum(bucket) / len(bucket)

    def snapshot(self) -> Dict[str, object]:
        now = time.perf_counter()
        with self._lock:
            self._trim_window(now)
            worker_avg_ms = sum(self._worker_ms) / len(self._worker_ms) if self._worker_ms else 0.0
            e2e_avg_ms = sum(self._e2e_ms) / len(self._e2e_ms) if self._e2e_ms else 0.0
            timing_avg_ms = {
                name: (sum(bucket) / len(bucket)) if bucket else 0.0
                for name, bucket in self._stage_ms.items()
            }
            timing_avg_ms["e2e_total"] = e2e_avg_ms
            timing_last_ms = dict(self._last_stage_ms)
            timing_last_ms["e2e_total"] = self._last_e2e_ms
            bottleneck_candidates = {
                key: timing_avg_ms.get(key, 0.0)
                for key in self._BOTTLENECK_STAGE_KEYS
                if timing_avg_ms.get(key, 0.0) > 0.0
            }
            if not bottleneck_candidates:
                bottleneck_candidates = {
                    key: value for key, value in timing_avg_ms.items() if key != "e2e_total" and value > 0.0
                }
            bottleneck_stage = ""
            bottleneck_avg_ms = 0.0
            if bottleneck_candidates:
                bottleneck_stage, bottleneck_avg_ms = max(
                    bottleneck_candidates.items(),
                    key=lambda item: item[1],
                )
            return {
                "source_id": self.source_id,
                "capture_count": self.capture_count,
                "dispatch_count": self.dispatch_count,
                "output_count": self.output_count,
                "worker_timeout_count": self.worker_timeout_count,
                "dropped_count": self.dropped_count,
                "dropped_rate": (self.dropped_count / self.capture_count) if self.capture_count else 0.0,
                "reorder_wait_count": self.reorder_wait_count,
                "reorder_skip_count": self.reorder_skip_count,
                "recognition_submit_count": self.recognition_submit_count,
                "recognition_result_count": self.recognition_result_count,
                "recognition_match_count": self.recognition_match_count,
                "recognition_reject_count": self.recognition_reject_count,
                "capture_fps": self._rate(self._capture_times),
                "dispatch_fps": self._rate(self._dispatch_times),
                "output_fps": self._rate(self._output_times),
                "worker_avg_ms": worker_avg_ms,
                "e2e_avg_ms": e2e_avg_ms,
                "timing_avg_ms": timing_avg_ms,
                "timing_last_ms": timing_last_ms,
                "last_pipeline_timing_ms": dict(self._last_pipeline_timing_ms),
                "bottleneck_stage": bottleneck_stage,
                "bottleneck_avg_ms": bottleneck_avg_ms,
                "queue_depths": dict(self._queue_depths),
                "queue_high_watermarks": dict(self._queue_high_watermarks),
                "drop_reasons": dict(self._drop_reasons),
                "recent_exception_count": len(self._exception_times),
            }


class MetricsStore:
    """Registry for per-source metrics."""

    def __init__(self, window_seconds: float = 5.0) -> None:
        self.window_seconds = float(window_seconds)
        self._lock = threading.Lock()
        self._sources: Dict[str, SourceMetrics] = {}

    def source(self, source_id: str) -> SourceMetrics:
        with self._lock:
            metrics = self._sources.get(source_id)
            if metrics is None:
                metrics = SourceMetrics(source_id=source_id, window_seconds=self.window_seconds)
                self._sources[source_id] = metrics
            return metrics

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {"sources": {source_id: metrics.snapshot() for source_id, metrics in self._sources.items()}}
