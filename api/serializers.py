"""Helpers for API payload shaping."""

from __future__ import annotations

from typing import Any
from urllib.parse import SplitResult, urlsplit

from pipelines.types import Detection, InferenceResult
from services.config import InputConfig, MediaMtxServiceConfig, OutputConfig, RuntimeConfig, infer_processing_mode


def serialize_detection(detection: Detection) -> dict[str, Any]:
    return {
        "label": detection.label,
        "score": float(detection.score),
        "bbox": list(detection.bbox),
        "attributes": dict(detection.attributes),
    }


def serialize_inference_result(result: InferenceResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "source_id": result.source_id,
        "frame_id": result.frame_id,
        "status": result.status.value,
        "worker_id": result.worker_id,
        "process_ms": float(result.process_ms),
        "error": result.error,
        "timing_ms": dict(result.timing_ms),
        "detections": [serialize_detection(item) for item in result.detections],
    }


def infer_public_host(host_header: str | None, fallback: str = "127.0.0.1") -> str:
    value = str(host_header or "").strip()
    if not value:
        return str(fallback)
    parsed = urlsplit(f"//{value}")
    return str(parsed.hostname or fallback)


def build_stream_urls(
    stream_url: str,
    public_host: str,
    mediamtx: MediaMtxServiceConfig | None = None,
) -> dict[str, str]:
    url = str(stream_url or "").strip()
    if not url:
        return {
            "publish_url": "",
            "rtsp_url": "",
            "hls_url": "",
            "webrtc_url": "",
            "rtmp_url": "",
            "path": "",
        }

    parsed: SplitResult = urlsplit(url)
    path = parsed.path.lstrip("/")
    if not parsed.scheme or not path:
        return {
            "publish_url": url,
            "rtsp_url": url,
            "hls_url": "",
            "webrtc_url": "",
            "rtmp_url": "",
            "path": "",
        }

    rtsp_port = parsed.port or (mediamtx.port if mediamtx is not None else 8554)
    host = str(public_host or parsed.hostname or "127.0.0.1")
    scheme = str(parsed.scheme or "rtsp").lower()
    base_rtsp = f"{scheme}://{host}:{rtsp_port}/{path}"
    return {
        "publish_url": url,
        "rtsp_url": base_rtsp,
        "hls_url": f"http://{host}:8888/{path}/index.m3u8",
        "webrtc_url": f"http://{host}:8889/{path}/",
        "rtmp_url": f"rtmp://{host}:1935/{path}",
        "path": path,
    }


def serialize_input_config(config: InputConfig) -> dict[str, Any]:
    return {
        "kind": config.kind,
        "device": config.device,
        "path": config.path,
        "backend": config.backend,
        "io_backend": config.io_backend,
        "width": int(config.width),
        "height": int(config.height),
        "fps": float(config.fps),
        "max_frames": int(config.max_frames),
        "buffer_size": int(config.buffer_size),
        "drop_initial_frames": int(config.drop_initial_frames),
        "convert_to_rgb": bool(config.convert_to_rgb),
        "rtsp_transport": config.rtsp_transport,
        "decoder": config.decoder,
    }


def serialize_output_config(config: OutputConfig) -> dict[str, Any]:
    return {
        "sink": config.sink,
        "stream_url": config.stream_url,
        "output_path": config.output_path,
        "fps": float(config.fps),
        "video_encoder": config.video_encoder,
        "video_bitrate": config.video_bitrate,
        "gop": int(config.gop),
        "rtsp_transport": config.rtsp_transport,
        "ffmpeg_path": config.ffmpeg_path,
        "ffmpeg_log_level": config.ffmpeg_log_level,
        "restart_backoff_ms": int(config.restart_backoff_ms),
        "max_restart_attempts": int(config.max_restart_attempts),
    }


def serialize_runtime_config(config: RuntimeConfig | None) -> dict[str, Any]:
    if config is None:
        return {}
    return {
        "source_id": config.source_id,
        "worker_mode": config.pipeline.worker_mode,
        "processing_mode": infer_processing_mode(config),
        "input": serialize_input_config(config.input),
        "output": serialize_output_config(config.output),
        "workers": {
            "count": int(config.workers.count),
            "task_timeout_ms": int(config.workers.task_timeout_ms),
        },
        "models": {
            "detector": config.models.detector,
            "classifier": config.models.classifier,
            "recognizer": config.models.recognizer,
        },
        "gallery": {
            "directory": config.gallery.directory,
            "max_images_per_identity": int(config.gallery.max_images_per_identity),
            "database": config.gallery.database,
            "samples_subdir": config.gallery.samples_subdir,
        },
        "api": {
            "host": config.api.host,
            "port": int(config.api.port),
            "debug": bool(config.api.debug),
            "auto_start": bool(config.api.auto_start),
            "enable_websocket": bool(config.api.enable_websocket),
        },
        "mediamtx": {
            "enabled": bool(config.mediamtx.enabled),
            "auto_start": bool(config.mediamtx.auto_start),
            "host": config.mediamtx.host,
            "port": int(config.mediamtx.port),
            "binary_path": config.mediamtx.binary_path,
            "config_path": config.mediamtx.config_path,
        },
        "alarm": {
            "enabled": bool(config.alarm.enabled),
            "image_dir": config.alarm.image_dir,
            "snapshot_interval_seconds": float(config.alarm.snapshot_interval_seconds),
            "leave_timeout_seconds": float(config.alarm.leave_timeout_seconds),
            "max_events": int(config.alarm.max_events),
        },
    }


def serialize_source_entry(
    config: RuntimeConfig | None,
    state: dict[str, Any],
    public_host: str,
) -> dict[str, Any]:
    if config is None:
        return {
            "source_id": "",
            "state": state.get("state", ""),
            "worker_mode": state.get("worker_mode", ""),
            "processing_mode": state.get("processing_mode", ""),
            "input": {},
            "output": {},
            "input_info": {},
            "output_info": {},
            "streams": {},
            "services": state.get("services", {}),
        }
    return {
        "source_id": config.source_id,
        "state": state.get("state", ""),
        "worker_mode": config.pipeline.worker_mode,
        "processing_mode": infer_processing_mode(config),
        "input": serialize_input_config(config.input),
        "output": serialize_output_config(config.output),
        "input_info": state.get("input_info", {}),
        "output_info": state.get("output_info", {}),
        "streams": build_stream_urls(config.output.stream_url, public_host=public_host, mediamtx=config.mediamtx),
        "services": state.get("services", {}),
    }
