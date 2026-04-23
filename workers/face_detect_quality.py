"""Stage-2 hot-path processor: detection + quality classification."""

from __future__ import annotations

import logging
import time

from adapters.face_detector import FaceDetectorAdapter
from adapters.face_quality import FaceQualityAdapter
from pipelines.types import Detection, InferenceResult, WorkerStatus, WorkerTask
from services.config import ClassifierConfig, DetectorConfig


LOGGER = logging.getLogger(__name__)


class FaceDetectQualityProcessor:
    """Per-worker processor that keeps detection and quality inline on the hot path."""

    def __init__(
        self,
        detector_model: str,
        classifier_model: str,
        detector_config: DetectorConfig,
        classifier_config: ClassifierConfig,
        core_id: int = 0,
        detector: FaceDetectorAdapter | None = None,
        classifier: FaceQualityAdapter | None = None,
    ) -> None:
        self.detector = detector or FaceDetectorAdapter(
            model_path=detector_model,
            config=detector_config,
            core_id=core_id,
        )
        self.classifier = classifier
        if self.classifier is None and classifier_model:
            self.classifier = FaceQualityAdapter(
                model_path=classifier_model,
                config=classifier_config,
                core_id=core_id,
            )

    def process(self, task: WorkerTask, worker_id: int) -> InferenceResult:
        started = time.perf_counter()
        detector_started = started
        faces = self.detector.detect(task.frame)
        detector_completed = time.perf_counter()
        detector_ms = (detector_completed - detector_started) * 1000.0
        classifier_ms = 0.0
        detections: list[Detection] = []
        for face in faces:
            quality_label = ""
            quality_score = 0.0
            if self.classifier is not None:
                try:
                    classifier_started = time.perf_counter()
                    quality = self.classifier.classify(task.frame, face.bbox)
                    classifier_ms += (time.perf_counter() - classifier_started) * 1000.0
                    quality_label = quality.label
                    quality_score = quality.score
                except Exception as exc:
                    LOGGER.warning(
                        "quality classifier failed on frame %s box %s: %s",
                        task.frame_id,
                        face.bbox,
                        exc,
                    )

            detections.append(
                Detection(
                    label="face",
                    score=float(face.score),
                    bbox=face.bbox,
                    attributes={
                        "model_source": "face_recognition",
                        "quality_label": quality_label,
                        "quality_score": float(quality_score),
                    },
                )
            )

        completed = time.perf_counter()
        total_ms = (completed - started) * 1000.0
        return InferenceResult(
            source_id=task.source_id,
            frame_id=task.frame_id,
            status=WorkerStatus.SUCCESS,
            detections=detections,
            worker_id=worker_id,
            started_monotonic=started,
            completed_monotonic=completed,
            process_ms=total_ms,
            timing_ms={
                "worker_process": total_ms,
                "detector": detector_ms,
                "classifier": classifier_ms,
            },
        )

    def close(self) -> None:
        try:
            self.detector.close()
        finally:
            if self.classifier is not None:
                self.classifier.close()
