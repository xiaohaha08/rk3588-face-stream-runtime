"""Runtime configuration dataclasses and JSON loading helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict


PROCESSING_MODE_FACE_RECOGNITION = "face_recognition"
FACE_RECOGNITION_WORKER_MODE = "face_detect_quality"
SUPPORTED_INPUT_KINDS = ("usb_camera", "rtsp", "video_file")
SUPPORTED_OUTPUT_SINKS = ("ffmpeg_rtsp", "ffmpeg_file")


@dataclass(frozen=True)
class InputConfig:
    kind: str = "usb_camera"
    width: int = 960
    height: int = 540
    fps: float = 18.0
    max_frames: int = 0
    device: str = "0"
    path: str = ""
    backend: str = ""
    io_backend: str = ""
    buffer_size: int = 1
    drop_initial_frames: int = 0
    convert_to_rgb: bool = True
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    rtsp_transport: str = "tcp"
    decoder: str = "auto"
    ffmpeg_log_level: str = "warning"
    restart_on_eof: bool = False
    restart_backoff_ms: int = 1200
    explicit_fields: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)


@dataclass(frozen=True)
class FrameStoreConfig:
    capacity: int = 96


@dataclass(frozen=True)
class SchedulerConfig:
    poll_interval_ms: int = 2


@dataclass(frozen=True)
class WorkerConfig:
    count: int = 2
    task_timeout_ms: int = 140
    result_queue_size: int = 32


@dataclass(frozen=True)
class ReorderConfig:
    max_wait_ms: int = 45


@dataclass(frozen=True)
class OutputConfig:
    push_queue_size: int = 8
    sink: str = "ffmpeg_rtsp"
    annotate_failures: bool = True
    stream_url: str = ""
    output_path: str = ""
    ffmpeg_path: str = "ffmpeg"
    fps: float = 0.0
    video_encoder: str = "libx264"
    video_bitrate: str = "4M"
    gop: int = 30
    rtsp_transport: str = "tcp"
    ffmpeg_log_level: str = "warning"
    x264_preset: str = "ultrafast"
    restart_backoff_ms: int = 1200
    max_restart_attempts: int = 0


@dataclass(frozen=True)
class MetricsConfig:
    window_seconds: float = 5.0
    log_interval_ms: int = 1000


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False
    auto_start: bool = True
    enable_websocket: bool = True
    event_buffer_size: int = 256
    recent_event_limit: int = 100


@dataclass(frozen=True)
class MediaMtxServiceConfig:
    enabled: bool = False
    auto_start: bool = False
    binary_path: str = ""
    config_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8554
    start_timeout_ms: int = 5000
    retry_interval_ms: int = 200


@dataclass(frozen=True)
class PipelineConfig:
    worker_mode: str = FACE_RECOGNITION_WORKER_MODE


@dataclass(frozen=True)
class ModelPathConfig:
    detector: str = ""
    classifier: str = ""
    recognizer: str = ""


@dataclass(frozen=True)
class DetectorConfig:
    input_size: int = 640
    obj_thresh: float = 0.25
    nms_thresh: float = 0.45


@dataclass(frozen=True)
class ClassifierConfig:
    input_size: int = 224
    crop_scale: float = 1.08
    center_shift_y: float = -0.03
    use_face_crop_direct_resize: bool = False
    softmax: bool = True
    preprocess: str = "raw_uint8"
    input_color: str = "rgb"
    input_scale: float = 1.0
    mean: tuple[float, float, float] = (123.675, 116.28, 103.53)
    std: tuple[float, float, float] = (58.395, 57.12, 57.375)
    labels: tuple[str, ...] = ("low", "verylow", "middle", "verymiddle", "high", "veryhigh")


@dataclass(frozen=True)
class RecognizerConfig:
    input_size: int = 160
    crop_margin: float = 0.15
    unknown_label: str = "unknown"
    preprocess: str = "raw_uint8"
    input_color: str = "rgb"
    input_scale: float = 1.0
    mean: tuple[float, float, float] = (127.5, 127.5, 127.5)
    std: tuple[float, float, float] = (127.5, 127.5, 127.5)
    recognition_quality_labels: tuple[str, ...] = ("middle", "verymiddle", "high", "veryhigh")
    match_threshold: float = 0.45


@dataclass(frozen=True)
class GalleryConfig:
    directory: str = "gallery"
    max_images_per_identity: int = 10
    database: str = ""
    samples_subdir: str = "face_library_samples"


@dataclass(frozen=True)
class TrackingConfig:
    iou_threshold: float = 0.35
    ttl_frames: int = 20
    recognition_cooldown_frames: int = 6
    recognition_retry_interval_seconds: float = 0.5
    recognizer_core_id: int = 2
    recognition_queue_size: int = 8


@dataclass(frozen=True)
class AlarmConfig:
    enabled: bool = False
    image_dir: str = "outputs/face_alarm_images"
    snapshot_interval_seconds: float = 5.0
    leave_timeout_seconds: float = 2.0
    max_events: int = 200


@dataclass(frozen=True)
class RuntimeConfig:
    source_id: str
    input: InputConfig
    frame_store: FrameStoreConfig
    scheduler: SchedulerConfig
    workers: WorkerConfig
    reorder: ReorderConfig
    output: OutputConfig
    metrics: MetricsConfig
    logging: LoggingConfig
    api: ApiConfig = field(default_factory=ApiConfig)
    mediamtx: MediaMtxServiceConfig = field(default_factory=MediaMtxServiceConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    models: ModelPathConfig = field(default_factory=ModelPathConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    recognizer: RecognizerConfig = field(default_factory=RecognizerConfig)
    gallery: GalleryConfig = field(default_factory=GalleryConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    alarm: AlarmConfig = field(default_factory=AlarmConfig)


def _section(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def _normalize_input_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value not in SUPPORTED_INPUT_KINDS:
        raise ValueError(f"unsupported input kind: {kind}")
    return value


def _normalize_output_sink(sink: str) -> str:
    value = str(sink or "").strip().lower()
    if value not in SUPPORTED_OUTPUT_SINKS:
        raise ValueError(f"unsupported output sink: {sink}")
    return value


def _normalize_worker_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value in {"", "face", "face_detect_quality", PROCESSING_MODE_FACE_RECOGNITION}:
        return FACE_RECOGNITION_WORKER_MODE
    raise ValueError(f"unsupported worker mode: {mode}")


def runtime_config_from_dict(data: Dict[str, Any]) -> RuntimeConfig:
    source_id = str(data.get("source_id", "cam-01") or "cam-01")
    input_data = _section(data, "input")
    output_data = _section(data, "output")
    pipeline_data = _section(data, "pipeline")
    model_data = _section(data, "models")
    classifier_data = _section(data, "classifier")
    recognizer_data = _section(data, "recognizer")
    input_kwargs = {
        **input_data,
        "kind": _normalize_input_kind(input_data.get("kind", InputConfig.kind)),
        "explicit_fields": frozenset(input_data.keys()),
    }
    output_kwargs = {
        **output_data,
        "sink": _normalize_output_sink(output_data.get("sink", OutputConfig.sink)),
    }
    return RuntimeConfig(
        source_id=source_id,
        input=InputConfig(**input_kwargs),
        frame_store=FrameStoreConfig(**_section(data, "frame_store")),
        scheduler=SchedulerConfig(**_section(data, "scheduler")),
        workers=WorkerConfig(**_section(data, "workers")),
        reorder=ReorderConfig(**_section(data, "reorder")),
        output=OutputConfig(**output_kwargs),
        metrics=MetricsConfig(**_section(data, "metrics")),
        logging=LoggingConfig(**_section(data, "logging")),
        api=ApiConfig(**_section(data, "api")),
        mediamtx=MediaMtxServiceConfig(**_section(data, "mediamtx")),
        pipeline=PipelineConfig(worker_mode=_normalize_worker_mode(pipeline_data.get("worker_mode", ""))),
        models=ModelPathConfig(
            detector=str(model_data.get("detector", "") or ""),
            classifier=str(model_data.get("classifier", "") or ""),
            recognizer=str(model_data.get("recognizer", "") or ""),
        ),
        detector=DetectorConfig(**_section(data, "detector")),
        classifier=ClassifierConfig(
            input_size=int(classifier_data.get("input_size", 224) or 224),
            crop_scale=float(classifier_data.get("crop_scale", 1.08) or 1.08),
            center_shift_y=float(classifier_data.get("center_shift_y", -0.03) or -0.03),
            use_face_crop_direct_resize=bool(classifier_data.get("use_face_crop_direct_resize", False)),
            softmax=bool(classifier_data.get("softmax", True)),
            preprocess=str(classifier_data.get("preprocess", "raw_uint8") or "raw_uint8"),
            input_color=str(classifier_data.get("input_color", "rgb") or "rgb"),
            input_scale=float(classifier_data.get("input_scale", 1.0) or 1.0),
            mean=tuple(classifier_data.get("mean", [123.675, 116.28, 103.53])),
            std=tuple(classifier_data.get("std", [58.395, 57.12, 57.375])),
            labels=tuple(classifier_data.get("labels", ["low", "verylow", "middle", "verymiddle", "high", "veryhigh"])),
        ),
        recognizer=RecognizerConfig(
            input_size=int(recognizer_data.get("input_size", 160) or 160),
            crop_margin=float(recognizer_data.get("crop_margin", 0.15) or 0.15),
            unknown_label=str(recognizer_data.get("unknown_label", "unknown") or "unknown"),
            preprocess=str(recognizer_data.get("preprocess", "raw_uint8") or "raw_uint8"),
            input_color=str(recognizer_data.get("input_color", "rgb") or "rgb"),
            input_scale=float(recognizer_data.get("input_scale", 1.0) or 1.0),
            mean=tuple(recognizer_data.get("mean", [127.5, 127.5, 127.5])),
            std=tuple(recognizer_data.get("std", [127.5, 127.5, 127.5])),
            recognition_quality_labels=tuple(
                recognizer_data.get("recognition_quality_labels", ["middle", "verymiddle", "high", "veryhigh"])
            ),
            match_threshold=float(recognizer_data.get("match_threshold", 0.45) or 0.45),
        ),
        gallery=GalleryConfig(**_section(data, "gallery")),
        tracking=TrackingConfig(**_section(data, "tracking")),
        alarm=AlarmConfig(**_section(data, "alarm")),
    )


def infer_processing_mode(_config: RuntimeConfig | None = None) -> str:
    return PROCESSING_MODE_FACE_RECOGNITION


def disable_face_recognition(config: RuntimeConfig) -> RuntimeConfig:
    return replace(
        config,
        models=replace(config.models, recognizer=""),
    )


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping: {config_path}")
    return runtime_config_from_dict(data)
