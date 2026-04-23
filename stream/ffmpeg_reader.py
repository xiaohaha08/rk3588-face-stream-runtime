"""Formal FFmpeg/RK3588 reader layer for RTSP and local files."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from stream.common import decoder_candidates, is_rtsp_url, read_exact, trim_tail
from stream.probe import StreamInfo, probe_stream_info


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FfmpegReaderConfig:
    input_url: str
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    rtsp_transport: str = "tcp"
    preferred_decoder: str = "auto"
    ffmpeg_log_level: str = "warning"
    output_pixel_format: str = "rgb24"
    restart_on_eof: bool = False
    restart_backoff_ms: int = 1200
    expected_width: int = 0
    expected_height: int = 0
    expected_fps: float = 0.0


class FfmpegFrameReader:
    """Read RGB/BGR raw frames from FFmpeg, preferring RK3588 hardware decoders."""

    _PIXEL_CHANNELS = {
        "rgb24": 3,
        "bgr24": 3,
    }

    def __init__(self, config: FfmpegReaderConfig) -> None:
        self.config = config
        self.stream_info = StreamInfo()
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._chosen_decoder = ""
        self._frame_size = 0
        self._shape: tuple[int, int, int] = (0, 0, 0)
        self._restart_attempts = 0
        self._stop_requested = False
        self._decoder_candidates: list[str] = []
        self._decoder_index = 0
        self._frames_from_current_process = 0

    def open(self) -> None:
        self._stop_requested = False
        self.stream_info = self._resolve_stream_info()
        self._decoder_candidates = decoder_candidates(self.stream_info.codec, self.config.preferred_decoder)
        self._decoder_index = 0
        pixel_format = str(self.config.output_pixel_format or "rgb24").strip().lower()
        channels = self._PIXEL_CHANNELS.get(pixel_format)
        if channels is None:
            raise ValueError(f"unsupported FFmpeg reader pixel format: {pixel_format}")
        self._shape = (int(self.stream_info.height), int(self.stream_info.width), int(channels))
        self._frame_size = int(np.prod(self._shape))
        self._start_process()

    def read(self) -> np.ndarray | None:
        if self._process is None:
            if self._stop_requested:
                return None
            raise RuntimeError("FFmpeg reader is not opened")

        while True:
            assert self._process is not None and self._process.stdout is not None
            raw = read_exact(self._process.stdout, self._frame_size)
            if len(raw) == self._frame_size:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(self._shape).copy()
                self._restart_attempts = 0
                self._frames_from_current_process += 1
                return frame

            if not self._maybe_restart():
                return None

    def close(self) -> None:
        self._stop_requested = True
        self._shutdown_process()

    def _shutdown_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass
        try:
            if process.stderr is not None:
                process.stderr.close()
        except Exception:
            pass
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    def _resolve_stream_info(self) -> StreamInfo:
        probed = probe_stream_info(
            input_url=self.config.input_url,
            ffprobe_path=self.config.ffprobe_path,
            transport=self.config.rtsp_transport,
        )
        width = int(probed.width or self.config.expected_width or 0)
        height = int(probed.height or self.config.expected_height or 0)
        fps = float(probed.fps or self.config.expected_fps or 25.0)
        codec = str(probed.codec or "").strip().lower()
        bitrate = int(probed.bitrate_kbps or 0)
        pix_fmt = str(probed.pix_fmt or "").strip().lower()
        duration_seconds = float(probed.duration_seconds or 0.0)
        if width <= 0 or height <= 0:
            raise RuntimeError(
                "ffprobe failed to determine input resolution; please provide input.width/input.height or check source"
            )
        return StreamInfo(
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            bitrate_kbps=bitrate,
            pix_fmt=pix_fmt,
            duration_seconds=duration_seconds,
        )

    def _start_process(self) -> None:
        last_error = ""
        for index in range(self._decoder_index, len(self._decoder_candidates)):
            decoder = self._decoder_candidates[index]
            command = self._build_command(decoder)
            LOGGER.info("FFmpeg reader starting: decoder=%s url=%s", decoder, self.config.input_url)
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    bufsize=self._frame_size * 2,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"ffmpeg not found: {self.config.ffmpeg_path}") from exc
            except Exception as exc:
                last_error = str(exc)
                continue

            self._process = process
            self._decoder_index = index
            self._chosen_decoder = decoder
            self._stderr_tail.clear()
            self._frames_from_current_process = 0
            if process.stderr is not None:
                threading.Thread(
                    target=self._stderr_logger,
                    args=(process,),
                    name=f"FfmpegReaderStderr-{decoder}",
                    daemon=True,
                ).start()
            return

        raise RuntimeError(f"failed to start FFmpeg reader: {last_error or 'no decoder candidate succeeded'}")

    def _maybe_restart(self) -> bool:
        tail = trim_tail(self._stderr_tail)
        self._shutdown_process()

        if self._stop_requested:
            return False
        if self._frames_from_current_process == 0 and self._decoder_index + 1 < len(self._decoder_candidates):
            self._decoder_index += 1
            LOGGER.warning(
                "FFmpeg reader switching decoder: previous=%s next=%s tail=%s",
                self._chosen_decoder or "unknown",
                self._decoder_candidates[self._decoder_index],
                tail or "<empty>",
            )
            try:
                self._start_process()
            except Exception:
                LOGGER.exception("FFmpeg reader decoder fallback failed")
                return False
            return True
        if not self.config.restart_on_eof:
            if tail:
                LOGGER.warning("FFmpeg reader reached EOF: %s", tail)
            return False

        self._restart_attempts += 1
        backoff = max(0.1, self.config.restart_backoff_ms / 1000.0)
        LOGGER.warning(
            "FFmpeg reader restarting after EOF: attempt=%s decoder=%s tail=%s",
            self._restart_attempts,
            self._chosen_decoder or "unknown",
            tail or "<empty>",
        )
        time.sleep(backoff)
        try:
            self._start_process()
        except Exception:
            LOGGER.exception("FFmpeg reader restart failed")
            return False
        return True

    def _build_command(self, decoder: str) -> list[str]:
        command = [
            str(self.config.ffmpeg_path or "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            str(self.config.ffmpeg_log_level or "warning"),
        ]
        if is_rtsp_url(self.config.input_url):
            command += [
                "-rtsp_transport",
                str(self.config.rtsp_transport or "tcp"),
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
            ]
        command += [
            "-c:v",
            str(decoder),
            "-i",
            str(self.config.input_url),
            "-an",
            "-sn",
            "-dn",
            "-pix_fmt",
            str(self.config.output_pixel_format or "rgb24"),
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        return command

    def _stderr_logger(self, process: subprocess.Popen[bytes]) -> None:
        try:
            assert process.stderr is not None
            for line in iter(process.stderr.readline, b""):
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                LOGGER.debug("reader ffmpeg: %s", text)
        except Exception:
            return
