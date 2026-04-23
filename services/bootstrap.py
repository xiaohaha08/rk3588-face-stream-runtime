"""Runtime assembly helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from inputs.base import BaseFrameSource
from inputs.opencv_capture import OpenCvFrameSource
from inputs.stream_capture import FfmpegFrameSource
from metrics.store import MetricsStore
from outputs.sink import BaseFrameSink, build_sink
from pipelines.source_runtime import SourceRuntime
from services.config import (
    FACE_RECOGNITION_WORKER_MODE,
    RuntimeConfig,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_input_config(config: RuntimeConfig):
    input_config = config.input
    if input_config.kind == "video_file" and str(input_config.path or "").strip():
        candidate = Path(input_config.path)
        if not candidate.is_absolute():
            input_config = replace(input_config, path=str((PROJECT_ROOT / candidate).resolve()))
    return input_config


def build_source(config: RuntimeConfig) -> BaseFrameSource:
    input_config = _resolve_input_config(config)
    if input_config.kind == "usb_camera":
        return OpenCvFrameSource(input_config)
    if input_config.kind in {"video_file", "rtsp"}:
        io_backend = str(input_config.io_backend or "").strip().lower()
        if io_backend in {"opencv", "cv2"}:
            return OpenCvFrameSource(input_config)
        return FfmpegFrameSource(input_config)
    raise ValueError(f"unsupported input source: {input_config.kind}")


def build_sink_for_runtime(config: RuntimeConfig) -> BaseFrameSink:
    return build_sink(config.output, fallback_fps=config.input.fps)


def _resolve_model_path(path_value: str) -> str:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return str(candidate)
    return str((PROJECT_ROOT / candidate).resolve())


def _resolve_directory_path(path_value: str) -> str:
    if not path_value:
        return ""
    candidate = Path(path_value)
    if candidate.is_absolute():
        return str(candidate)
    return str((PROJECT_ROOT / candidate).resolve())


def build_processor_factory(config: RuntimeConfig):
    worker_mode = str(config.pipeline.worker_mode or "").strip().lower()
    if worker_mode != FACE_RECOGNITION_WORKER_MODE:
        raise ValueError(f"unsupported worker mode: {config.pipeline.worker_mode}")

    detector_model = _resolve_model_path(config.models.detector) if config.models.detector else ""
    classifier_model = _resolve_model_path(config.models.classifier) if config.models.classifier else ""
    if not detector_model:
        raise ValueError("face recognition runtime requires models.detector")

    def _factory(worker_id: int):
        from workers.face_detect_quality import FaceDetectQualityProcessor

        return FaceDetectQualityProcessor(
            detector_model=detector_model,
            classifier_model=classifier_model,
            detector_config=config.detector,
            classifier_config=config.classifier,
            core_id=worker_id,
        )

    return _factory


def build_result_enricher(config: RuntimeConfig, source_metrics=None):
    recognizer_model = str(config.models.recognizer or "").strip()
    if not recognizer_model:
        return None

    detector_model = _resolve_model_path(config.models.detector) if config.models.detector else ""
    recognizer_model = _resolve_model_path(recognizer_model)
    if not detector_model:
        raise ValueError("face recognition runtime requires models.detector")

    from adapters.face_detector import FaceDetectorAdapter
    from adapters.face_embedding import FaceEmbeddingAdapter
    from services.gallery_store import GalleryStore
    from services.recognition import AsyncFaceRecognizer, TrackAwareRecognitionCoordinator

    gallery_config = replace(config.gallery, directory=_resolve_directory_path(config.gallery.directory))
    gallery_detector = FaceDetectorAdapter(
        model_path=detector_model,
        config=config.detector,
        core_id=config.tracking.recognizer_core_id,
    )
    gallery_embedder = FaceEmbeddingAdapter(
        model_path=recognizer_model,
        config=config.recognizer,
        core_id=config.tracking.recognizer_core_id,
    )
    try:
        gallery_store = GalleryStore.load_from_directory(
            gallery_config=gallery_config,
            detector=gallery_detector,
            embedder=gallery_embedder,
        )
    finally:
        gallery_detector.close()
        gallery_embedder.close()

    recognizer = None
    if gallery_store.identity_count() > 0:
        recognizer = AsyncFaceRecognizer(
            embedder=FaceEmbeddingAdapter(
                model_path=recognizer_model,
                config=config.recognizer,
                core_id=config.tracking.recognizer_core_id,
            ),
            gallery_store=gallery_store,
            threshold=config.recognizer.match_threshold,
            unknown_label=config.recognizer.unknown_label,
            queue_size=config.tracking.recognition_queue_size,
            source_metrics=source_metrics,
        )
    coordinator = TrackAwareRecognitionCoordinator(
        recognizer=recognizer,
        quality_label_order=config.classifier.labels,
        trigger_quality_labels=config.recognizer.recognition_quality_labels,
        unknown_label=config.recognizer.unknown_label,
        track_iou_threshold=config.tracking.iou_threshold,
        track_ttl_frames=config.tracking.ttl_frames,
        recognition_cooldown_frames=config.tracking.recognition_cooldown_frames,
        recognition_retry_interval_seconds=config.tracking.recognition_retry_interval_seconds,
        crop_margin=config.recognizer.crop_margin,
    )
    coordinator.gallery_identity_count = gallery_store.identity_count()
    coordinator.recognizer_enabled = recognizer is not None
    return coordinator


def build_alarm_store(config: RuntimeConfig, event_bus=None):
    from services.face_alarm import FaceAlarmStore

    image_root = _resolve_directory_path(config.alarm.image_dir) or str((PROJECT_ROOT / "outputs" / "face_alarm_images").resolve())
    return FaceAlarmStore(
        source_id=config.source_id,
        image_root=image_root,
        unknown_label=config.recognizer.unknown_label,
        snapshot_interval_seconds=config.alarm.snapshot_interval_seconds,
        leave_timeout_seconds=config.alarm.leave_timeout_seconds,
        max_events=config.alarm.max_events,
        enabled=bool(config.alarm.enabled),
        event_publisher=event_bus,
    )


def build_runtime(config: RuntimeConfig, event_bus=None) -> SourceRuntime:
    source = build_source(config)
    sink = build_sink_for_runtime(config)
    processor_factory = build_processor_factory(config)
    metrics_store = MetricsStore(window_seconds=config.metrics.window_seconds)
    source_metrics = metrics_store.source(config.source_id)
    result_enricher = build_result_enricher(config, source_metrics=source_metrics)
    alarm_store = build_alarm_store(config, event_bus=event_bus)
    try:
        return SourceRuntime(
            config=config,
            source=source,
            sink=sink,
            metrics_store=metrics_store,
            processor_factory=processor_factory,
            result_enricher=result_enricher,
            alarm_store=alarm_store,
            event_publisher=event_bus,
        )
    except Exception:
        close_fn = getattr(result_enricher, "close", None)
        if callable(close_fn):
            close_fn()
        close_alarm = getattr(alarm_store, "close", None)
        if callable(close_alarm):
            close_alarm()
        raise
