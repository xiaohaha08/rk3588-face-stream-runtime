"""Backward-compatible wrapper around the formal FFmpeg publisher layer."""

from __future__ import annotations

from dataclasses import dataclass

from stream.ffmpeg_publisher import FfmpegFramePublisher, FfmpegPublisherConfig


@dataclass(frozen=True)
class FfmpegRtspConfig:
    ffmpeg_path: str = "ffmpeg"
    stream_url: str = ""
    fps: float = 15.0
    video_encoder: str = "libx264"
    video_bitrate: str = "4M"
    gop: int = 30
    rtsp_transport: str = "tcp"
    ffmpeg_log_level: str = "warning"
    x264_preset: str = "ultrafast"
    input_pixel_format: str = "rgb24"
    restart_backoff_ms: int = 1200
    max_restart_attempts: int = 0


class FfmpegRtspPublisher(FfmpegFramePublisher):
    """Compatibility alias kept for the current sink layer."""

    def __init__(self, config: FfmpegRtspConfig) -> None:
        super().__init__(
            config=FfmpegPublisherConfig(
                ffmpeg_path=config.ffmpeg_path,
                output_url=config.stream_url,
                fps=config.fps,
                video_encoder=config.video_encoder,
                video_bitrate=config.video_bitrate,
                gop=config.gop,
                rtsp_transport=config.rtsp_transport,
                ffmpeg_log_level=config.ffmpeg_log_level,
                x264_preset=config.x264_preset,
                input_pixel_format=config.input_pixel_format,
                restart_backoff_ms=config.restart_backoff_ms,
                max_restart_attempts=config.max_restart_attempts,
            )
        )
