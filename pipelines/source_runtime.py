"""Single-source runtime for the production face recognition pipeline."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, Optional

from inputs.base import BaseFrameSource
from metrics.store import MetricsStore
from outputs.composer import FrameComposer
from outputs.sink import BaseFrameSink
from pipelines.reorder import WeakOrderReorderer
from pipelines.types import CapturedFrame, InferenceResult, PushPacket, WorkerStatus, WorkerTask
from schedulers.latest_wins import LatestFrameSlot, RoundRobinWorkerSelector
from services.config import RuntimeConfig
from utils.queues import put_drop_oldest
from workers.runtime_worker import WorkerThread


LOGGER = logging.getLogger(__name__)


class FrameStore:
    """Bounded store for original frames, keyed by frame_id."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._frames: "OrderedDict[int, CapturedFrame]" = OrderedDict()

    def put(self, frame: CapturedFrame) -> int:
        evicted = 0
        with self._lock:
            self._frames[frame.frame_id] = frame
            while len(self._frames) > self.capacity:
                self._frames.popitem(last=False)
                evicted += 1
        return evicted

    def get(self, frame_id: int) -> Optional[CapturedFrame]:
        with self._lock:
            return self._frames.get(frame_id)

    def pop(self, frame_id: int) -> Optional[CapturedFrame]:
        with self._lock:
            return self._frames.pop(frame_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._frames)


class SourceRuntime:
    """Single-source runtime for face detection, quality, and recognition."""

    def __init__(
        self,
        config: RuntimeConfig,
        source: BaseFrameSource,
        sink: BaseFrameSink,
        metrics_store: MetricsStore | None = None,
        processor_factory=None,
        result_enricher=None,
        alarm_store=None,
        event_publisher=None,
    ) -> None:
        if processor_factory is None:
            raise ValueError("processor_factory is required")

        self.config = config
        self.source = source
        self.sink = sink
        self.metrics_store = metrics_store or MetricsStore(window_seconds=config.metrics.window_seconds)
        self.source_metrics = self.metrics_store.source(config.source_id)
        self.frame_store = FrameStore(config.frame_store.capacity)
        self.latest_slot: LatestFrameSlot[CapturedFrame] = LatestFrameSlot()
        self.result_queue: Queue = Queue(maxsize=max(1, config.workers.result_queue_size))
        self.push_queue: Queue = Queue(maxsize=max(1, config.output.push_queue_size))
        self.reorderer = WeakOrderReorderer(config.source_id, config.reorder.max_wait_ms)
        self.composer = FrameComposer(annotate_failures=config.output.annotate_failures)
        self.selector = RoundRobinWorkerSelector()
        self.processor_factory = processor_factory
        self.result_enricher = result_enricher
        self.alarm_store = alarm_store
        self.event_publisher = event_publisher
        self.logger = logging.getLogger(f"runtime.{config.source_id}")
        self.stop_event = threading.Event()
        self.capture_finished = threading.Event()
        self.output_done = threading.Event()
        self._latest_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._input_info_lock = threading.Lock()
        self._input_info = self._default_input_info(opened=False)
        self._output_info: dict[str, object] = {
            "target_fps": self._configured_output_fps(),
            "fps_cap": 30.0,
            "mode": "input_fps_capped",
        }
        self._latest_captured_frame_id = -1
        self._next_push_deadline = 0.0
        self._started = False
        self._stopped = False
        self._threads: Dict[str, threading.Thread] = {}
        self._workers = [
            WorkerThread(
                worker_id=index,
                worker_config=config.workers,
                processor=self._build_processor(index),
                result_queue=self.result_queue,
                source_metrics=self.source_metrics,
            )
            for index in range(config.workers.count)
        ]

    def _build_processor(self, worker_id: int):
        processor = self.processor_factory(worker_id)
        if processor is None:
            raise ValueError(f"processor_factory returned None for worker {worker_id}")
        return processor

    def _set_latest_captured_frame_id(self, frame_id: int) -> None:
        with self._latest_lock:
            self._latest_captured_frame_id = max(self._latest_captured_frame_id, int(frame_id))

    def _get_latest_captured_frame_id(self) -> int:
        with self._latest_lock:
            return self._latest_captured_frame_id

    def _default_input_info(self, opened: bool) -> dict[str, object]:
        config = self.config.input
        return {
            "kind": str(config.kind or ""),
            "target": str(config.path or config.device or ""),
            "backend": str(config.backend or ""),
            "io_backend": str(config.io_backend or ""),
            "opened": bool(opened),
            "width": int(config.width or 0),
            "height": int(config.height or 0),
            "fps": float(config.fps or 0.0),
            "bitrate_kbps": 0,
            "codec": "",
            "pixel_format": "",
            "frame_count": int(config.max_frames or 0),
            "duration_seconds": 0.0,
        }

    def _set_input_info(self, info: dict[str, object]) -> None:
        with self._input_info_lock:
            self._input_info = {**self._input_info, **dict(info or {})}

    def _refresh_input_info(self, frame=None) -> None:
        info = self._default_input_info(opened=True)
        snapshot_info = getattr(self.source, "snapshot_info", None)
        if callable(snapshot_info):
            try:
                info.update(dict(snapshot_info() or {}))
            except Exception:
                self.source_metrics.record_exception()
        if frame is not None:
            height, width = frame.shape[:2]
            info["width"] = int(width)
            info["height"] = int(height)
        self._set_input_info(info)

    def snapshot_input_info(self) -> dict[str, object]:
        with self._input_info_lock:
            info = dict(self._input_info)
        metrics = self.source_metrics.snapshot()
        observed_fps = float(metrics.get("capture_fps", 0.0) or 0.0)
        info["observed_fps"] = observed_fps
        info["effective_fps"] = self._effective_input_fps(info=info, metrics=metrics)
        return info

    def snapshot_output_info(self) -> dict[str, object]:
        target_fps = self._target_output_fps()
        with self._input_info_lock:
            self._output_info = {**self._output_info, "target_fps": target_fps}
            return dict(self._output_info)

    def _configured_output_fps(self) -> float:
        input_fps = float(self.config.input.fps or 0.0)
        cap = min(30.0, input_fps) if input_fps > 0.0 else 30.0
        configured = float(self.config.output.fps or 0.0)
        if configured > 0.0:
            return min(cap, configured)
        return cap

    def _effective_input_fps(
        self,
        info: dict[str, object] | None = None,
        metrics: dict[str, object] | None = None,
    ) -> float:
        if info is None:
            with self._input_info_lock:
                info = dict(self._input_info)
        reported_fps = float(info.get("fps", 0.0) or 0.0)
        configured_fps = float(self.config.input.fps or 0.0)
        metrics_snapshot = metrics if metrics is not None else self.source_metrics.snapshot()
        observed_fps = float(metrics_snapshot.get("capture_fps", 0.0) or 0.0)
        capture_count = int(metrics_snapshot.get("capture_count", 0) or 0)

        if capture_count >= 5 and observed_fps > 0.1:
            if reported_fps <= 0.0 or observed_fps < reported_fps * 0.9:
                return observed_fps
        if reported_fps > 0.0:
            return reported_fps
        if configured_fps > 0.0:
            return configured_fps
        return observed_fps

    def _target_output_fps(self) -> float:
        effective_input_fps = self._effective_input_fps()
        cap = min(30.0, effective_input_fps) if effective_input_fps > 0.0 else 30.0
        output_config_fps = float(self.config.output.fps or 0.0)
        if output_config_fps > 0.0:
            return min(cap, output_config_fps)
        return cap

    def _all_workers_idle(self) -> bool:
        return all(worker.is_idle() for worker in self._workers)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._publish_event("runtime.state", {"state": "running"})
        for worker in self._workers:
            worker.start()
        self._threads = {
            "capture": threading.Thread(target=self._capture_loop, name=f"Capture-{self.config.source_id}", daemon=True),
            "scheduler": threading.Thread(target=self._scheduler_loop, name=f"Scheduler-{self.config.source_id}", daemon=True),
            "output": threading.Thread(target=self._output_loop, name=f"Output-{self.config.source_id}", daemon=True),
            "push": threading.Thread(target=self._push_loop, name=f"Push-{self.config.source_id}", daemon=True),
            "metrics": threading.Thread(target=self._metrics_loop, name=f"Metrics-{self.config.source_id}", daemon=True),
        }
        for thread in self._threads.values():
            thread.start()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._publish_event("runtime.state", {"state": "stopping"})
        self.stop_event.set()
        try:
            self.source.close()
        except Exception:
            self.source_metrics.record_exception()
        for worker in self._workers:
            worker.stop()
        for worker in self._workers:
            worker.join(timeout=2.0)
        for thread in self._threads.values():
            thread.join(timeout=2.0)
        close_fn = getattr(self.result_enricher, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                self.source_metrics.record_exception()
        close_alarm = getattr(self.alarm_store, "close", None)
        if callable(close_alarm):
            try:
                close_alarm()
            except Exception:
                self.source_metrics.record_exception()
        self._publish_event("runtime.state", {"state": "stopped"})

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else (time.perf_counter() + float(timeout))
        while not self.output_done.is_set():
            if deadline is not None and time.perf_counter() >= deadline:
                self.stop()
                return False
            time.sleep(0.05)
        self.stop()
        return True

    def snapshot_metrics(self) -> Dict[str, object]:
        return self.metrics_store.snapshot()

    def snapshot_alarms(self, limit: int = 100, active_only: bool = False) -> list[dict[str, object]]:
        store = self.alarm_store
        if store is None:
            return []
        return store.recent(limit=limit, active_only=active_only)

    def get_alarm(self, alarm_id: int) -> dict[str, object] | None:
        store = self.alarm_store
        if store is None:
            return None
        return store.get(alarm_id)

    def resolve_alarm_image(self, relative_path: str) -> Path | None:
        store = self.alarm_store
        if store is None:
            return None
        return store.resolve_image_path(relative_path)

    def set_alarm_enabled(self, enabled: bool) -> bool:
        store = self.alarm_store
        if store is None:
            return False
        set_enabled = getattr(store, "set_enabled", None)
        if callable(set_enabled):
            return bool(set_enabled(bool(enabled)))
        return False

    def set_face_recognition_enabled(self, enabled: bool) -> bool:
        enricher = self.result_enricher
        if enricher is None:
            return False
        set_enabled = getattr(enricher, "set_enabled", None)
        if callable(set_enabled):
            return bool(set_enabled(bool(enabled)))
        return False

    def set_face_identity_enabled(self, identity: str, enabled: bool) -> bool:
        updated = False
        enricher = self.result_enricher
        if enricher is not None:
            set_identity_enabled = getattr(enricher, "set_identity_enabled", None)
            if callable(set_identity_enabled):
                updated = bool(set_identity_enabled(identity, bool(enabled)))
        store = self.alarm_store
        if store is not None:
            set_identity_alarm = getattr(store, "set_identity_enabled", None)
            if callable(set_identity_alarm):
                set_identity_alarm(identity, bool(enabled))
        return updated

    def _publish_event(self, topic: str, payload: dict[str, object]) -> None:
        publisher = self.event_publisher
        if publisher is None:
            return
        try:
            publisher.publish(topic=topic, source_id=self.config.source_id, payload=payload)
        except Exception:
            self.logger.exception("event publish failed: %s", topic)

    def _capture_loop(self) -> None:
        frame_id = 0
        try:
            self.source.open()
            self._refresh_input_info()
            self._publish_event("input.info", {"input": self.snapshot_input_info(), "output": self.snapshot_output_info()})
            while not self.stop_event.is_set():
                read_started = time.perf_counter()
                frame = self.source.read()
                read_completed = time.perf_counter()
                if frame is None:
                    break
                self._refresh_input_info(frame=frame)
                now_mono = read_completed
                capture_read_ms = max(0.0, (read_completed - read_started) * 1000.0)
                captured = CapturedFrame(
                    source_id=self.config.source_id,
                    frame_id=frame_id,
                    capture_monotonic=now_mono,
                    capture_wall_time=time.time(),
                    frame=frame,
                    timing_ms={"capture_read": capture_read_ms},
                )
                evicted = self.frame_store.put(captured)
                for _ in range(evicted):
                    self.source_metrics.record_drop("frame_store_evicted")
                replaced = self.latest_slot.put_latest(captured)
                if replaced is not None:
                    self.source_metrics.record_drop("latest_slot_overwrite")
                self._set_latest_captured_frame_id(frame_id)
                self.source_metrics.record_capture(now_mono)
                self.source_metrics.observe_stage_timing("capture_read", capture_read_ms)
                self.source_metrics.observe_queue("frame_store", self.frame_store.size())
                self.source_metrics.observe_queue("latest_slot", self.latest_slot.size())
                frame_id += 1
        except Exception:
            self.source_metrics.record_exception()
            self.logger.exception("capture loop failed")
            self._publish_event("runtime.error", {"component": "capture", "error": "capture loop failed"})
        finally:
            self.capture_finished.set()
            try:
                self.source.close()
            except Exception:
                self.source_metrics.record_exception()

    def _scheduler_loop(self) -> None:
        poll_interval = max(0.001, self.config.scheduler.poll_interval_ms / 1000.0)
        while not self.stop_event.is_set():
            self.source_metrics.observe_queue("latest_slot", self.latest_slot.size())
            self.source_metrics.observe_queue("result_queue", self.result_queue.qsize())
            if self.capture_finished.is_set() and self.latest_slot.empty() and self._all_workers_idle():
                return
            worker = self.selector.pick_idle(self._workers)
            if worker is None:
                time.sleep(poll_interval)
                continue
            frame = self.latest_slot.pop_latest()
            if frame is None:
                time.sleep(poll_interval)
                continue
            task = WorkerTask(
                source_id=frame.source_id,
                frame_id=frame.frame_id,
                capture_monotonic=frame.capture_monotonic,
                capture_wall_time=frame.capture_wall_time,
                frame=frame.frame,
                deadline_monotonic=frame.capture_monotonic + (self.config.workers.task_timeout_ms / 1000.0),
                timing_ms=dict(frame.timing_ms),
            )
            if not worker.submit_if_idle(task):
                replaced = self.latest_slot.put_latest(frame)
                if replaced is not None and replaced.frame_id != frame.frame_id:
                    self.source_metrics.record_drop("scheduler_restore_overwrite")
                time.sleep(poll_interval)
                continue
            self.source_metrics.record_dispatch()
            self.source_metrics.observe_queue("latest_slot", self.latest_slot.size())

    def _drain_results(self) -> None:
        while True:
            try:
                result: InferenceResult = self.result_queue.get_nowait()
            except Empty:
                break
            self.source_metrics.observe_queue("result_queue", self.result_queue.qsize())
            accepted = self.reorderer.add_result(result)
            if not accepted:
                self.source_metrics.record_drop("late_result")

    def _output_loop(self) -> None:
        while not self.stop_event.is_set():
            self._drain_results()
            latest_frame_id = self._get_latest_captured_frame_id()
            decisions = self.reorderer.drain_ready(
                latest_frame_id=latest_frame_id,
                now_monotonic=time.perf_counter(),
                frame_get=self.frame_store.get,
                frame_pop=self.frame_store.pop,
                metrics=self.source_metrics,
            )
            for decision in decisions:
                result = decision.result
                timing_ms = dict(decision.frame.timing_ms)
                if result is not None:
                    timing_ms.update(result.timing_ms)
                output_started = time.perf_counter()
                output_wait_ms = 0.0
                if result is not None and result.completed_monotonic > 0.0:
                    output_wait_ms = max(0.0, (output_started - result.completed_monotonic) * 1000.0)
                timing_ms["output_wait"] = output_wait_ms
                self.source_metrics.observe_stage_timing("output_wait", output_wait_ms)
                enrich_ms = 0.0
                if self.result_enricher is not None:
                    try:
                        enrich_started = time.perf_counter()
                        result = self.result_enricher.enrich(decision.frame, result)
                        enrich_ms = max(0.0, (time.perf_counter() - enrich_started) * 1000.0)
                    except Exception:
                        self.source_metrics.record_exception()
                        self.logger.exception("result enricher failed on frame %s", decision.frame.frame_id)
                        self._publish_event(
                            "runtime.error",
                            {"component": "result_enricher", "frame_id": decision.frame.frame_id, "error": "result enricher failed"},
                        )
                timing_ms["enrich"] = enrich_ms
                self.source_metrics.observe_stage_timing("enrich", enrich_ms)
                status = result.status if result is not None else WorkerStatus.EXPIRED
                compose_started = time.perf_counter()
                composed = self.composer.compose(decision.frame.frame, result, decision.reason)
                compose_ms = max(0.0, (time.perf_counter() - compose_started) * 1000.0)
                timing_ms["compose"] = compose_ms
                self.source_metrics.observe_stage_timing("compose", compose_ms)
                alarm_ms = 0.0
                if self.alarm_store is not None:
                    try:
                        alarm_started = time.perf_counter()
                        self.alarm_store.process_frame(decision.frame, result, snapshot_frame=composed)
                        alarm_ms = max(0.0, (time.perf_counter() - alarm_started) * 1000.0)
                    except Exception:
                        self.source_metrics.record_exception()
                        self.logger.exception("alarm store failed on frame %s", decision.frame.frame_id)
                        self._publish_event(
                            "runtime.error",
                            {"component": "alarm_store", "frame_id": decision.frame.frame_id, "error": "alarm store failed"},
                        )
                timing_ms["alarm"] = alarm_ms
                self.source_metrics.observe_stage_timing("alarm", alarm_ms)
                output_prepare_ms = enrich_ms + compose_ms + alarm_ms
                timing_ms["output_prepare"] = output_prepare_ms
                self.source_metrics.observe_stage_timing("output_prepare", output_prepare_ms)
                packet_ready_monotonic = time.perf_counter()
                target_output_fps = self._target_output_fps()
                packet = PushPacket(
                    source_id=self.config.source_id,
                    frame_id=decision.frame.frame_id,
                    capture_monotonic=decision.frame.capture_monotonic,
                    frame=composed,
                    reason=decision.reason,
                    status=status,
                    ready_monotonic=packet_ready_monotonic,
                    target_output_fps=target_output_fps,
                    timing_ms=timing_ms,
                )
                if not put_drop_oldest(self.push_queue, packet):
                    self.source_metrics.record_drop("push_queue_reject")
                self.source_metrics.observe_queue("frame_store", self.frame_store.size())
                self.source_metrics.observe_queue("push_queue", self.push_queue.qsize())
                detections = []
                if result is not None:
                    detections = [
                        {
                            "label": item.label,
                            "score": float(item.score),
                            "bbox": list(item.bbox),
                            "attributes": dict(item.attributes),
                        }
                        for item in result.detections
                    ]
                self._publish_event(
                    "inference.result",
                    {
                        "frame_id": decision.frame.frame_id,
                        "reason": decision.reason,
                        "status": status.value,
                        "detection_count": len(detections),
                        "detections": detections,
                    },
                )

            if (
                self.capture_finished.is_set()
                and self.latest_slot.empty()
                and self._all_workers_idle()
                and self.result_queue.empty()
                and self.reorderer.next_frame_id > latest_frame_id
            ):
                self.output_done.set()
                return
            time.sleep(0.002)

        self.output_done.set()

    def _push_loop(self) -> None:
        try:
            self.sink.open()
            while True:
                if (self.output_done.is_set() or self.stop_event.is_set()) and self.push_queue.empty():
                    return
                try:
                    packet: PushPacket = self.push_queue.get(timeout=0.1)
                except Empty:
                    continue
                target_output_fps = float(packet.target_output_fps or self._target_output_fps() or 0.0)
                with self._input_info_lock:
                    self._output_info = {**self._output_info, "target_fps": target_output_fps}
                if target_output_fps > 0.0:
                    interval = 1.0 / max(0.1, target_output_fps)
                    now_for_rate = time.perf_counter()
                    if self._next_push_deadline <= 0.0 or now_for_rate - self._next_push_deadline > interval:
                        self._next_push_deadline = now_for_rate
                    wait_seconds = self._next_push_deadline - now_for_rate
                    if wait_seconds > 0.0:
                        if not self.push_queue.empty():
                            self.source_metrics.record_drop("output_fps_cap")
                            self.source_metrics.observe_queue("push_queue", self.push_queue.qsize())
                            continue
                        time.sleep(wait_seconds)
                        if not self.push_queue.empty():
                            self.source_metrics.record_drop("output_fps_cap")
                            self.source_metrics.observe_queue("push_queue", self.push_queue.qsize())
                            continue
                    self._next_push_deadline = max(self._next_push_deadline, time.perf_counter()) + interval
                dequeue_started = time.perf_counter()
                push_queue_wait_ms = 0.0
                if packet.ready_monotonic > 0.0:
                    push_queue_wait_ms = max(0.0, (dequeue_started - packet.ready_monotonic) * 1000.0)
                self.source_metrics.observe_stage_timing("push_queue_wait", push_queue_wait_ms)
                write_started = time.perf_counter()
                self.sink.write(packet)
                write_completed = time.perf_counter()
                push_write_ms = max(0.0, (write_completed - write_started) * 1000.0)
                self.source_metrics.observe_stage_timing("push_write", push_write_ms)
                timing_ms = dict(packet.timing_ms)
                timing_ms["push_queue_wait"] = push_queue_wait_ms
                timing_ms["push_write"] = push_write_ms
                self.source_metrics.record_output(packet.capture_monotonic, now=write_completed, timing_ms=timing_ms)
                self.source_metrics.observe_queue("push_queue", self.push_queue.qsize())
        except Exception:
            self.source_metrics.record_exception()
            self.logger.exception("push loop failed")
            self._publish_event("runtime.error", {"component": "push", "error": "push loop failed"})
        finally:
            try:
                self.sink.close()
            except Exception:
                self.source_metrics.record_exception()

    def _metrics_loop(self) -> None:
        interval = max(0.1, self.config.metrics.log_interval_ms / 1000.0)
        while not self.stop_event.is_set():
            time.sleep(interval)
            snapshot = self.source_metrics.snapshot()
            self.logger.info("metrics: %s", json.dumps(snapshot, ensure_ascii=False))
            self._publish_event("metrics.update", snapshot)
            self._publish_event("input.info", {"input": self.snapshot_input_info(), "output": self.snapshot_output_info()})
            if self.output_done.is_set():
                return
