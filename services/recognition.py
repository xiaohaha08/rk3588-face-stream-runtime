"""异步识别侧车与轻量轨迹协调器。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Sequence

import numpy as np

from adapters.face_embedding import FaceEmbeddingAdapter
from metrics.store import SourceMetrics
from pipelines.types import CapturedFrame, Detection, InferenceResult, WorkerStatus
from services.gallery_store import GalleryStore
from utils.queues import put_drop_oldest


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecognitionTask:
    track_id: int
    frame_id: int
    face_rgb: np.ndarray
    quality_label: str
    quality_score: float


@dataclass(frozen=True)
class RecognitionResult:
    track_id: int
    frame_id: int
    identity: str
    similarity: float
    identity_enabled: bool
    quality_label: str
    quality_score: float


@dataclass
class TrackState:
    track_id: int
    last_box: tuple[int, int, int, int]
    last_frame_id: int
    identity: str
    similarity: float
    identity_enabled: bool
    recognition_pending: bool = False
    last_submit_monotonic: float = -1.0e12
    last_submit_frame: int = -10_000_000
    last_submit_rank: int = -1
    last_submit_score: float = 0.0
    best_frame_id: int = -1
    best_quality_label: str = ""
    best_quality_rank: int = -1
    best_quality_score: float = 0.0
    best_candidate_rgb: np.ndarray | None = None


def _quality_rank(label: str, label_order: Sequence[str]) -> int:
    text = str(label or "").strip().lower()
    for index, item in enumerate(label_order):
        if text == str(item or "").strip().lower():
            return index
    return -1


def _box_iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    lx1, ly1, lx2, ly2 = [int(v) for v in left]
    rx1, ry1, rx2, ry2 = [int(v) for v in right]
    inter_x1 = max(lx1, rx1)
    inter_y1 = max(ly1, ry1)
    inter_x2 = min(lx2, rx2)
    inter_y2 = min(ly2, ry2)
    inter_w = max(0, inter_x2 - inter_x1)
    inter_h = max(0, inter_y2 - inter_y1)
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    inter = float(inter_w * inter_h)
    left_area = float(max(1, lx2 - lx1) * max(1, ly2 - ly1))
    right_area = float(max(1, rx2 - rx1) * max(1, ry2 - ry1))
    denom = left_area + right_area - inter
    if denom <= 1e-6:
        return 0.0
    return inter / denom


class AsyncFaceRecognizer:
    """单线程异步识别器，不阻塞主输出链。"""

    def __init__(
        self,
        embedder: FaceEmbeddingAdapter,
        gallery_store: GalleryStore,
        threshold: float,
        unknown_label: str,
        queue_size: int = 8,
        source_metrics: SourceMetrics | None = None,
    ) -> None:
        self.embedder = embedder
        self.gallery_store = gallery_store
        self.threshold = float(threshold)
        self.unknown_label = str(unknown_label or "unknown")
        self.source_metrics = source_metrics
        self._task_queue: Queue = Queue(maxsize=max(1, int(queue_size)))
        self.result_queue: Queue = Queue(maxsize=max(4, int(queue_size) * 2))
        self._pending_tracks: set[int] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="AsyncFaceRecognizer", daemon=True)
        self._thread.start()

    def submit(
        self,
        track_id: int,
        frame_id: int,
        face_rgb: np.ndarray,
        quality_label: str,
        quality_score: float,
    ) -> bool:
        if self._stop_event.is_set():
            return False
        with self._lock:
            if track_id in self._pending_tracks:
                if self.source_metrics is not None:
                    self.source_metrics.record_recognition_reject()
                return False

        task = RecognitionTask(
            track_id=int(track_id),
            frame_id=int(frame_id),
            face_rgb=np.ascontiguousarray(face_rgb),
            quality_label=str(quality_label or ""),
            quality_score=float(quality_score or 0.0),
        )
        try:
            self._task_queue.put_nowait(task)
        except Full:
            if self.source_metrics is not None:
                self.source_metrics.record_recognition_reject()
            return False

        with self._lock:
            self._pending_tracks.add(task.track_id)
        if self.source_metrics is not None:
            self.source_metrics.record_recognition_submit()
            self.source_metrics.observe_queue("recognition_queue", self._task_queue.qsize())
        return True

    def poll_result(self) -> RecognitionResult | None:
        try:
            result = self.result_queue.get_nowait()
        except Empty:
            return None
        if self.source_metrics is not None:
            matched = str(result.identity or "") != self.unknown_label and float(result.similarity) > 0.0
            self.source_metrics.record_recognition_result(matched=matched)
            self.source_metrics.observe_queue("recognition_result_queue", self.result_queue.qsize())
        return result

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._task_queue.put_nowait(None)
        except Full:
            pass
        self._thread.join(timeout=5.0)
        self.embedder.close()

    def _put_result(self, result: RecognitionResult) -> None:
        if not put_drop_oldest(self.result_queue, result) and self.source_metrics is not None:
            self.source_metrics.record_recognition_reject()
        if self.source_metrics is not None:
            self.source_metrics.observe_queue("recognition_result_queue", self.result_queue.qsize())

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._task_queue.get(timeout=0.2)
            except Empty:
                continue
            if task is None:
                break
            try:
                started = time.perf_counter()
                embedding = self.embedder.extract_from_crop(task.face_rgb)
                extract_completed = time.perf_counter()
                identity, similarity = self.gallery_store.match(
                    embedding=embedding,
                    threshold=self.threshold,
                    unknown_label=self.unknown_label,
                )
                completed = time.perf_counter()
                if self.source_metrics is not None:
                    self.source_metrics.observe_stage_timing(
                        "recognition_extract",
                        (extract_completed - started) * 1000.0,
                    )
                    self.source_metrics.observe_stage_timing(
                        "recognition_match",
                        (completed - extract_completed) * 1000.0,
                    )
                    self.source_metrics.observe_stage_timing(
                        "recognition_total",
                        (completed - started) * 1000.0,
                    )
                self._put_result(
                    RecognitionResult(
                        track_id=task.track_id,
                        frame_id=task.frame_id,
                        identity=identity,
                        similarity=similarity,
                        identity_enabled=self.gallery_store.is_enabled(identity),
                        quality_label=task.quality_label,
                        quality_score=task.quality_score,
                    )
                )
            except Exception:
                LOGGER.exception("异步识别失败，track_id=%s", task.track_id)
            finally:
                with self._lock:
                    self._pending_tracks.discard(task.track_id)
                if self.source_metrics is not None:
                    self.source_metrics.observe_queue("recognition_queue", self._task_queue.qsize())


class TrackAwareRecognitionCoordinator:
    """在输出侧维护轻量轨迹，并按质量触发异步识别。"""

    def __init__(
        self,
        recognizer: AsyncFaceRecognizer | None,
        quality_label_order: Sequence[str],
        trigger_quality_labels: Sequence[str],
        unknown_label: str = "unknown",
        track_iou_threshold: float = 0.35,
        track_ttl_frames: int = 20,
        recognition_cooldown_frames: int = 6,
        recognition_retry_interval_seconds: float = 0.5,
        crop_margin: float = 0.15,
    ) -> None:
        self._lock = threading.RLock()
        self.recognizer = recognizer
        self.recognizer_enabled = recognizer is not None
        self.gallery_identity_count = 0
        self.quality_label_order = list(quality_label_order or [])
        self.unknown_label = str(unknown_label or "unknown")
        self.trigger_quality_labels = {
            str(label or "").strip().lower()
            for label in trigger_quality_labels
            if str(label or "").strip()
        }
        self.track_iou_threshold = float(track_iou_threshold)
        self.track_ttl_frames = max(1, int(track_ttl_frames))
        self.recognition_cooldown_frames = max(1, int(recognition_cooldown_frames))
        self.recognition_retry_interval_seconds = max(0.0, float(recognition_retry_interval_seconds))
        self.crop_margin = float(crop_margin)
        self._tracks: dict[int, TrackState] = {}
        self._next_track_id = 1

    def close(self) -> None:
        if self.recognizer is not None:
            self.recognizer.close()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            can_enable = self.recognizer is not None
            self.recognizer_enabled = bool(enabled) and can_enable
            if not self.recognizer_enabled:
                self._tracks.clear()
            return self.recognizer_enabled

    def set_identity_enabled(self, identity: str, enabled: bool) -> bool:
        with self._lock:
            recognizer = self.recognizer
            if recognizer is None:
                return False
            updated = recognizer.gallery_store.set_enabled(identity, enabled)
            if not updated:
                return False
            name = str(identity or "").strip()
            for track in self._tracks.values():
                if str(track.identity or "").strip() == name:
                    track.identity_enabled = bool(enabled)
            return True

    def enrich(self, frame: CapturedFrame, result: InferenceResult | None) -> InferenceResult | None:
        with self._lock:
            self._drain_recognition_results()
            self._cleanup_stale_tracks(frame.frame_id)
            if result is None or result.status != WorkerStatus.SUCCESS:
                return result

            if not self.recognizer_enabled:
                enriched_detections: list[Detection] = []
                for detection in result.detections:
                    attrs = dict(detection.attributes)
                    if str(detection.label or "").strip().lower() == "face":
                        attrs["identity"] = self.unknown_label
                        attrs["similarity"] = 0.0
                        attrs["identity_enabled"] = False
                    enriched_detections.append(
                        Detection(
                            label=detection.label,
                            score=detection.score,
                            bbox=detection.bbox,
                            attributes=attrs,
                        )
                    )
                result.detections = enriched_detections
                return result

            used_track_ids: set[int] = set()
            enriched_detections: list[Detection] = []
            for detection in result.detections:
                track_id = self._assign_track_id(detection.bbox, frame.frame_id, used_track_ids)
                used_track_ids.add(track_id)
                track = self._tracks[track_id]
                track.last_box = detection.bbox
                track.last_frame_id = frame.frame_id

                attrs = dict(detection.attributes)
                quality_label = str(attrs.get("quality_label", "") or "")
                quality_score = float(attrs.get("quality_score", 0.0) or 0.0)
                self._maybe_submit_recognition(
                    track=track,
                    frame=frame,
                    bbox=detection.bbox,
                    quality_label=quality_label,
                    quality_score=quality_score,
                )
                track.identity_enabled = self._identity_enabled(track.identity)
                attrs["track_id"] = track_id
                attrs["identity"] = track.identity or self.unknown_label
                attrs["similarity"] = float(track.similarity)
                attrs["identity_enabled"] = bool(track.identity_enabled)
                enriched_detections.append(
                    Detection(
                        label=detection.label,
                        score=detection.score,
                        bbox=detection.bbox,
                        attributes=attrs,
                    )
                )

            result.detections = enriched_detections
            return result

    def _drain_recognition_results(self) -> None:
        if self.recognizer is None:
            return
        while True:
            result = self.recognizer.poll_result()
            if result is None:
                return
            track = self._tracks.get(result.track_id)
            if track is None:
                continue
            track.identity = result.identity
            track.similarity = float(result.similarity)
            track.identity_enabled = bool(result.identity_enabled)
            track.recognition_pending = False
            track.last_submit_rank = _quality_rank(result.quality_label, self.quality_label_order)
            track.last_submit_score = float(result.quality_score)

    def _cleanup_stale_tracks(self, frame_id: int) -> None:
        stale_ids = [
            track_id
            for track_id, track in self._tracks.items()
            if frame_id - track.last_frame_id > self.track_ttl_frames
        ]
        for track_id in stale_ids:
            self._tracks.pop(track_id, None)

    def _assign_track_id(
        self,
        bbox: tuple[int, int, int, int],
        frame_id: int,
        used_track_ids: set[int],
    ) -> int:
        best_track_id = -1
        best_iou = 0.0
        for track_id, track in self._tracks.items():
            if track_id in used_track_ids:
                continue
            if frame_id - track.last_frame_id > self.track_ttl_frames:
                continue
            iou = _box_iou(bbox, track.last_box)
            if iou >= self.track_iou_threshold and iou > best_iou:
                best_iou = iou
                best_track_id = track_id
        if best_track_id >= 0:
            return best_track_id

        track_id = self._next_track_id
        self._next_track_id += 1
        self._tracks[track_id] = TrackState(
            track_id=track_id,
            last_box=bbox,
            last_frame_id=frame_id,
            identity=self.unknown_label,
            similarity=0.0,
            identity_enabled=False,
        )
        return track_id

    def _identity_enabled(self, identity: str) -> bool:
        recognizer = self.recognizer
        if recognizer is None:
            return False
        return bool(recognizer.gallery_store.is_enabled(identity))

    def _recognition_interval_elapsed(self, track: TrackState, frame: CapturedFrame) -> bool:
        if self.recognition_retry_interval_seconds > 0.0:
            return (float(frame.capture_monotonic) - float(track.last_submit_monotonic)) >= self.recognition_retry_interval_seconds
        return (frame.frame_id - track.last_submit_frame) >= self.recognition_cooldown_frames

    def _maybe_submit_recognition(
        self,
        track: TrackState,
        frame: CapturedFrame,
        bbox: tuple[int, int, int, int],
        quality_label: str,
        quality_score: float,
    ) -> None:
        if self.recognizer is None:
            return
        quality_key = str(quality_label or "").strip().lower()
        if quality_key not in self.trigger_quality_labels:
            return

        quality_rank = _quality_rank(quality_key, self.quality_label_order)
        if quality_rank < 0:
            return

        from adapters.common import crop_with_margin

        crop = crop_with_margin(frame.frame, bbox, margin_ratio=self.crop_margin)
        if crop is None:
            return

        improved = (
            track.best_candidate_rgb is None
            or quality_rank > track.best_quality_rank
            or (quality_rank == track.best_quality_rank and float(quality_score) >= track.best_quality_score)
        )
        if improved:
            track.best_quality_rank = quality_rank
            track.best_quality_label = quality_key
            track.best_quality_score = float(quality_score)
            track.best_frame_id = frame.frame_id
            track.best_candidate_rgb = np.ascontiguousarray(crop)

        if track.best_candidate_rgb is None or track.recognition_pending:
            return
        if not self._recognition_interval_elapsed(track, frame):
            return

        submitted = self.recognizer.submit(
            track_id=track.track_id,
            frame_id=track.best_frame_id,
            face_rgb=track.best_candidate_rgb,
            quality_label=track.best_quality_label,
            quality_score=track.best_quality_score,
        )
        if submitted:
            track.recognition_pending = True
            track.last_submit_frame = frame.frame_id
            track.last_submit_monotonic = float(frame.capture_monotonic)
            track.last_submit_rank = track.best_quality_rank
            track.last_submit_score = track.best_quality_score
            track.best_frame_id = -1
            track.best_quality_label = ""
            track.best_quality_rank = -1
            track.best_quality_score = 0.0
            track.best_candidate_rgb = None
