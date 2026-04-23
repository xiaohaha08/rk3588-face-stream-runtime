"""Formal FFmpeg publishing layer for RTSP push on RK3588."""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

from stream.common import trim_tail


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FfmpegPublisherConfig:
    ffmpeg_path: str = "ffmpeg"
    output_url: str = ""
    fps: float = 15.0
    video_encoder: str = "libx264"
    video_bitrate: str = "4M"
    gop: int = 30
    rtsp_transport: str = "tcp"
    ffmpeg_log_level: str = "warning"
    x264_preset: str = "ultrafast"
    input_pixel_format: str = "rgb24"
    output_format: str = ""
    restart_backoff_ms: int = 1200
    max_restart_attempts: int = 0


class FfmpegFramePublisher:
    """Publish raw frames through FFmpeg, with bounded restart behavior."""

    def __init__(self, config: FfmpegPublisherConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._frame_width = 0
        self._frame_height = 0
        self._restart_count = 0
        self._stderr_tail: deque[str] = deque(maxlen=40)

    def open(self) -> None:
        if not self.config.output_url:
            raise ValueError("ffmpeg publisher requires output_url")

    def write(self, frame) -> None:
        self.write_frame(frame)

    def write_frame(self, frame, fps: float | None = None) -> None:
        height, width = frame.shape[:2]
        if fps is not None and float(fps) > 0.0 and self._process is None:
            self.config = replace(self.config, fps=max(0.1, float(fps)))
        if self._process is None:
            self._start_process(width=width, height=height)
        elif width != self._frame_width or height != self._frame_height:
            raise RuntimeError(
                f"frame size changed during publishing: {width}x{height} != {self._frame_width}x{self._frame_height}"
            )

        assert self._process is not None and self._process.stdin is not None
        if self._process.poll() is not None:
            self._restart_or_raise(width=width, height=height, reason=f"ffmpeg exited with code {self._process.returncode}")

        try:
            self._process.stdin.write(memoryview(frame).cast("B"))
            self._restart_count = 0
        except BrokenPipeError:
            self._restart_or_raise(width=width, height=height, reason="ffmpeg stdin is closed")
            assert self._process is not None and self._process.stdin is not None
            self._process.stdin.write(memoryview(frame).cast("B"))

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

    def _restart_or_raise(self, width: int, height: int, reason: str) -> None:
        self._restart_count += 1
        max_restarts = int(self.config.max_restart_attempts or 0)
        if max_restarts > 0 and self._restart_count > max_restarts:
            raise RuntimeError(f"ffmpeg publisher restart limit exceeded: {reason}")
        tail = trim_tail(self._stderr_tail)
        LOGGER.warning("FFmpeg publisher restarting: reason=%s tail=%s", reason, tail or "<empty>")
        self.close()
        time.sleep(max(0.1, self.config.restart_backoff_ms / 1000.0))
        self._start_process(width=width, height=height)

    def _start_process(self, width: int, height: int) -> None:
        self._frame_width = int(width)
        self._frame_height = int(height)
        output_url = str(self.config.output_url or "").strip()
        if output_url and "://" not in output_url:
            Path(output_url).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        command = self._build_command(width=self._frame_width, height=self._frame_height)
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"ffmpeg not found: {self.config.ffmpeg_path}") from exc

    def _build_command(self, width: int, height: int) -> list[str]:
        fps = max(0.1, float(self.config.fps))
        command = [
            str(self.config.ffmpeg_path or "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            str(self.config.ffmpeg_log_level or "warning"),
            "-y",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "rawvideo",
            "-pix_fmt",
            str(self.config.input_pixel_format or "rgb24"),
            "-s:v",
            f"{width}x{height}",
            "-r",
            f"{fps:.3f}",
            "-i",
            "pipe:0",
            "-an",
        ]

        encoder = str(self.config.video_encoder or "libx264").strip()
        if encoder == "libx264":
            command += [
                "-c:v",
                "libx264",
                "-preset",
                str(self.config.x264_preset or "ultrafast"),
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
            ]
        else:
            command += [
                "-c:v",
                encoder,
                "-pix_fmt",
                "yuv420p",
            ]

        if self.config.video_bitrate:
            command += ["-b:v", str(self.config.video_bitrate)]
        if int(self.config.gop or 0) > 0:
            command += ["-g", str(int(self.config.gop))]

        output_url = str(self.config.output_url or "")
        if output_url.lower().startswith("rtsp://"):
            command += [
                "-f",
                "rtsp",
                "-rtsp_transport",
                str(self.config.rtsp_transport or "tcp"),
                output_url,
            ]
        elif output_url.lower().startswith("udp://"):
            command += ["-f", "mpegts", output_url]
        else:
            file_format = self._resolve_file_format(output_url)
            if file_format == "mp4":
                command += ["-movflags", "+faststart"]
            command += ["-f", file_format, output_url]
        return command

    def _resolve_file_format(self, output_url: str) -> str:
        explicit = str(self.config.output_format or "").strip().lower()
        if explicit and explicit != "auto":
            return explicit
        suffix = Path(output_url).suffix.lower()
        if suffix == ".mp4":
            return "mp4"
        if suffix in {".mkv", ".mk3d", ".mka", ".mks"}:
            return "matroska"
        if suffix == ".avi":
            return "avi"
        if suffix == ".mov":
            return "mov"
        return "mpegts"
