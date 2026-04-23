"""OpenCV-backed real input sources for USB camera, local files, and RTSP."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any

import numpy as np

from inputs.base import BaseFrameSource
from services.config import InputConfig


def _import_cv2():
    try:
        import cv2  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("opencv-python-headless/cv2 is required for real input sources") from exc
    return cv2


@dataclass(frozen=True)
class OpenCvSourceSpec:
    target: int | str
    backend: int | None


class OpenCvFrameSource(BaseFrameSource):
    """Read frames from OpenCV capture backends and normalize them to RGB."""

    def __init__(self, config: InputConfig) -> None:
        self.config = config
        self._capture: Any = None
        self._frame_index = 0
        self._info_lock = threading.Lock()
        self._source_info: dict[str, Any] = self._base_info(opened=False)

    def open(self) -> None:
        cv2 = _import_cv2()
        spec = self._build_source_spec(cv2)
        if spec.backend is None:
            self._capture = cv2.VideoCapture(spec.target)
        else:
            self._capture = cv2.VideoCapture(spec.target, spec.backend)
        if not self._capture or not self._capture.isOpened():
            raise RuntimeError(f"failed to open input source: kind={self.config.kind} target={spec.target!r}")

        self._set_optional_property(cv2, "CAP_PROP_BUFFERSIZE", self.config.buffer_size)
        if self.config.width > 0:
            self._set_optional_property(cv2, "CAP_PROP_FRAME_WIDTH", self.config.width)
        if self.config.height > 0:
            self._set_optional_property(cv2, "CAP_PROP_FRAME_HEIGHT", self.config.height)
        if self.config.fps > 0:
            self._set_optional_property(cv2, "CAP_PROP_FPS", self.config.fps)

        self._frame_index = 0
        self._update_source_info(cv2=cv2, spec=spec, opened=True)
        for _ in range(max(0, int(self.config.drop_initial_frames))):
            ok, _frame = self._capture.read()
            if not ok:
                break

    def read(self) -> np.ndarray | None:
        if self.config.max_frames > 0 and self._frame_index >= self.config.max_frames:
            return None
        if self._capture is None:
            raise RuntimeError("input source is not opened")

        cv2 = _import_cv2()
        ok, frame = self._capture.read()
        if not ok or frame is None:
            return None

        if bool(self.config.convert_to_rgb):
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        self._frame_index += 1
        self._update_frame_shape(frame)
        return np.ascontiguousarray(frame)

    def close(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            finally:
                self._capture = None
                with self._info_lock:
                    self._source_info = {**self._source_info, "opened": False}

    def snapshot_info(self) -> dict[str, Any]:
        with self._info_lock:
            return dict(self._source_info)

    def _build_source_spec(self, cv2) -> OpenCvSourceSpec:
        kind = str(self.config.kind or "").strip().lower()
        backend_name = str(self.config.backend or "").strip().lower()
        backend = self._resolve_backend(cv2, backend_name)

        if kind == "usb_camera":
            target_text = str(self.config.device or "0").strip()
            target: int | str = int(target_text) if target_text.isdigit() else target_text
            return OpenCvSourceSpec(target=target, backend=backend)

        if kind == "video_file":
            path_value = str(self.config.path or "").strip()
            if not path_value:
                raise ValueError("video_file input requires input.path")
            return OpenCvSourceSpec(target=str(Path(path_value).expanduser()), backend=backend)

        if kind == "rtsp":
            uri = str(self.config.path or "").strip()
            if not uri:
                raise ValueError("rtsp input requires input.path")
            return OpenCvSourceSpec(target=uri, backend=backend)

        raise NotImplementedError(f"unsupported OpenCV input kind: {self.config.kind}")

    @staticmethod
    def _resolve_backend(cv2, backend_name: str) -> int | None:
        if not backend_name:
            return None
        mapping = {
            "any": "CAP_ANY",
            "v4l2": "CAP_V4L2",
            "ffmpeg": "CAP_FFMPEG",
            "gstreamer": "CAP_GSTREAMER",
        }
        attr_name = mapping.get(backend_name)
        if not attr_name:
            raise ValueError(f"unsupported input backend: {backend_name}")
        if not hasattr(cv2, attr_name):
            raise RuntimeError(f"current OpenCV build does not expose backend {backend_name}")
        return int(getattr(cv2, attr_name))

    def _set_optional_property(self, cv2, attr_name: str, value: float | int) -> None:
        if value is None:
            return
        if not hasattr(cv2, attr_name):
            return
        try:
            prop_id = getattr(cv2, attr_name)
            self._capture.set(prop_id, float(value))
        except Exception:
            return

    def _base_info(self, opened: bool) -> dict[str, Any]:
        target = str(self.config.path or self.config.device or "").strip()
        return {
            "kind": str(self.config.kind or ""),
            "target": target,
            "backend": str(self.config.backend or ""),
            "io_backend": "opencv",
            "opened": bool(opened),
            "width": int(self.config.width or 0),
            "height": int(self.config.height or 0),
            "fps": float(self.config.fps or 0.0),
            "bitrate_kbps": 0,
            "codec": "",
            "pixel_format": "",
            "frame_count": int(self.config.max_frames or 0),
            "duration_seconds": 0.0,
        }

    def _update_source_info(self, cv2, spec: OpenCvSourceSpec, opened: bool) -> None:
        info = self._base_info(opened=opened)
        info["target"] = str(spec.target)
        info["backend"] = str(self.config.backend or "")
        if self._capture is None:
            with self._info_lock:
                self._source_info = info
            return

        width = self._get_capture_property(cv2, "CAP_PROP_FRAME_WIDTH")
        height = self._get_capture_property(cv2, "CAP_PROP_FRAME_HEIGHT")
        fps = self._get_capture_property(cv2, "CAP_PROP_FPS")
        bitrate = self._get_capture_property(cv2, "CAP_PROP_BITRATE")
        frame_count = self._get_capture_property(cv2, "CAP_PROP_FRAME_COUNT")
        fourcc = self._get_capture_property(cv2, "CAP_PROP_FOURCC")
        if width > 0:
            info["width"] = int(round(width))
        if height > 0:
            info["height"] = int(round(height))
        if fps > 0:
            info["fps"] = float(fps)
        if bitrate > 0:
            info["bitrate_kbps"] = int(round(bitrate))
        if frame_count > 0:
            info["frame_count"] = int(round(frame_count))
        if fourcc > 0:
            info["codec"] = self._decode_fourcc(int(round(fourcc)))
        with self._info_lock:
            self._source_info = info

    def _update_frame_shape(self, frame: np.ndarray) -> None:
        height, width = frame.shape[:2]
        with self._info_lock:
            self._source_info = {
                **self._source_info,
                "width": int(width),
                "height": int(height),
                "opened": True,
            }

    def _get_capture_property(self, cv2, attr_name: str) -> float:
        if self._capture is None or not hasattr(cv2, attr_name):
            return 0.0
        try:
            return float(self._capture.get(getattr(cv2, attr_name)) or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _decode_fourcc(value: int) -> str:
        chars = []
        for shift in (0, 8, 16, 24):
            code = (int(value) >> shift) & 0xFF
            if 32 <= code <= 126:
                chars.append(chr(code))
        return "".join(chars).strip()
