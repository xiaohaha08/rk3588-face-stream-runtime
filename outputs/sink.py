"""Frame sink abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pipelines.types import PushPacket
from services.config import OutputConfig
from stream.ffmpeg_publisher import FfmpegFramePublisher, FfmpegPublisherConfig


class BaseFrameSink(ABC):
    def open(self) -> None:
        """Acquire sink resources."""

    @abstractmethod
    def write(self, packet: PushPacket) -> None:
        """Write one frame packet."""

    def close(self) -> None:
        """Release sink resources."""


def _resolve_output_fps(config: OutputConfig, fallback_fps: float) -> float:
    configured_fps = float(config.fps or 0.0)
    if configured_fps > 0:
        return configured_fps
    return max(0.1, float(fallback_fps))


class FfmpegRtspFrameSink(BaseFrameSink):
    """Push composed RGB frames through the formal FFmpeg publisher layer."""

    def __init__(self, config: OutputConfig, fallback_fps: float = 15.0) -> None:
        target_fps = _resolve_output_fps(config, fallback_fps)
        self.publisher = FfmpegFramePublisher(
            FfmpegPublisherConfig(
                ffmpeg_path=config.ffmpeg_path,
                output_url=config.stream_url,
                fps=target_fps,
                video_encoder=config.video_encoder,
                video_bitrate=config.video_bitrate,
                gop=config.gop,
                rtsp_transport=config.rtsp_transport,
                ffmpeg_log_level=config.ffmpeg_log_level,
                x264_preset=config.x264_preset,
                output_format="rtsp",
                restart_backoff_ms=config.restart_backoff_ms,
                max_restart_attempts=config.max_restart_attempts,
            )
        )

    def open(self) -> None:
        self.publisher.open()

    def write(self, packet: PushPacket) -> None:
        self.publisher.write_frame(packet.frame, fps=packet.target_output_fps or None)

    def close(self) -> None:
        self.publisher.close()


class FfmpegFileFrameSink(BaseFrameSink):
    """Write processed frames to a local video file through FFmpeg."""

    def __init__(self, config: OutputConfig, fallback_fps: float = 15.0) -> None:
        output_path = str(config.output_path or "").strip()
        if not output_path:
            raise ValueError("ffmpeg_file sink requires output.output_path")
        target_fps = _resolve_output_fps(config, fallback_fps)
        self.publisher = FfmpegFramePublisher(
            FfmpegPublisherConfig(
                ffmpeg_path=config.ffmpeg_path,
                output_url=output_path,
                output_format="auto",
                fps=target_fps,
                video_encoder=config.video_encoder,
                video_bitrate=config.video_bitrate,
                gop=config.gop,
                ffmpeg_log_level=config.ffmpeg_log_level,
                x264_preset=config.x264_preset,
                restart_backoff_ms=config.restart_backoff_ms,
                max_restart_attempts=config.max_restart_attempts,
            )
        )

    def open(self) -> None:
        self.publisher.open()

    def write(self, packet: PushPacket) -> None:
        self.publisher.write_frame(packet.frame, fps=packet.target_output_fps or None)

    def close(self) -> None:
        self.publisher.close()


def build_sink(config: OutputConfig, fallback_fps: float = 15.0) -> BaseFrameSink:
    resolved = str(config.sink or "").strip().lower()
    if resolved == "ffmpeg_rtsp":
        return FfmpegRtspFrameSink(config=config, fallback_fps=fallback_fps)
    if resolved == "ffmpeg_file":
        return FfmpegFileFrameSink(config=config, fallback_fps=fallback_fps)
    raise ValueError(f"unsupported output sink: {config.sink}")
