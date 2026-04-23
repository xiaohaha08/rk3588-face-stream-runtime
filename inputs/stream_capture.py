"""FFmpeg-backed real input source for RTSP and local video files."""

from __future__ import annotations

from typing import Any

import numpy as np

from inputs.base import BaseFrameSource
from services.config import InputConfig
from stream.ffmpeg_reader import FfmpegFrameReader, FfmpegReaderConfig


class FfmpegFrameSource(BaseFrameSource):
    """Decode RTSP/local files through FFmpeg while preferring RK3588 hardware codecs."""

    def __init__(self, config: InputConfig) -> None:
        self.config = config
        self._reader: FfmpegFrameReader | None = None
        self._frame_index = 0
        self._source_info: dict[str, Any] = self._base_info(opened=False)

    def open(self) -> None:
        input_path = str(self.config.path or "").strip()
        if not input_path:
            raise ValueError(f"{self.config.kind} input requires input.path")
        pixel_format = "rgb24" if bool(self.config.convert_to_rgb) else "bgr24"
        restart_on_eof = bool(self.config.restart_on_eof)
        if self.config.kind == "rtsp" and "restart_on_eof" not in self.config.explicit_fields:
            restart_on_eof = True
        self._reader = FfmpegFrameReader(
            FfmpegReaderConfig(
                input_url=input_path,
                ffmpeg_path=self.config.ffmpeg_path,
                ffprobe_path=self.config.ffprobe_path,
                rtsp_transport=self.config.rtsp_transport,
                preferred_decoder=self.config.decoder,
                ffmpeg_log_level=self.config.ffmpeg_log_level,
                output_pixel_format=pixel_format,
                restart_on_eof=restart_on_eof,
                restart_backoff_ms=self.config.restart_backoff_ms,
                expected_width=self.config.width,
                expected_height=self.config.height,
                expected_fps=self.config.fps,
            )
        )
        self._reader.open()
        self._source_info = self._info_from_reader(opened=True)
        self._frame_index = 0

    def read(self) -> np.ndarray | None:
        if self._reader is None:
            raise RuntimeError("FFmpeg frame source is not opened")
        if self.config.max_frames > 0 and self._frame_index >= self.config.max_frames:
            return None
        frame = self._reader.read()
        if frame is None:
            return None
        self._frame_index += 1
        return np.ascontiguousarray(frame)

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None
                self._source_info = {**self._source_info, "opened": False}

    def snapshot_info(self) -> dict[str, Any]:
        return dict(self._source_info)

    def _base_info(self, opened: bool) -> dict[str, Any]:
        return {
            "kind": str(self.config.kind or ""),
            "target": str(self.config.path or ""),
            "backend": "",
            "io_backend": "ffmpeg",
            "opened": bool(opened),
            "width": int(self.config.width or 0),
            "height": int(self.config.height or 0),
            "fps": float(self.config.fps or 0.0),
            "bitrate_kbps": 0,
            "codec": "",
            "pixel_format": "",
            "frame_count": int(self.config.max_frames or 0),
            "duration_seconds": 0.0,
            "decoder": str(self.config.decoder or ""),
            "rtsp_transport": str(self.config.rtsp_transport or ""),
        }

    def _info_from_reader(self, opened: bool) -> dict[str, Any]:
        info = self._base_info(opened=opened)
        reader = self._reader
        if reader is None:
            return info
        stream_info = reader.stream_info
        info.update(
            {
                "width": int(stream_info.width or info["width"] or 0),
                "height": int(stream_info.height or info["height"] or 0),
                "fps": float(stream_info.fps or info["fps"] or 0.0),
                "bitrate_kbps": int(stream_info.bitrate_kbps or 0),
                "codec": str(stream_info.codec or ""),
                "pixel_format": str(stream_info.pix_fmt or ""),
                "duration_seconds": float(stream_info.duration_seconds or 0.0),
                "decoder": str(getattr(reader, "_chosen_decoder", "") or self.config.decoder or ""),
            }
        )
        return info
