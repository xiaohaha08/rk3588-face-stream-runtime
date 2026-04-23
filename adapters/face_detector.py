"""Face detector adapter for the RKNN YOLOv8 face model."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from adapters.common import clip_box, letterbox_rgb
from adapters.rknn_runtime import ModelRuntime, RKNNModelRunner
from adapters.yolov8_face_postprocess import yolov8_post_process
from services.config import DetectorConfig


@dataclass(frozen=True)
class DetectedFace:
    bbox: tuple[int, int, int, int]
    score: float


class FaceDetectorAdapter:
    """Detector wrapper that returns face boxes in source-frame coordinates."""

    def __init__(
        self,
        model_path: str,
        config: DetectorConfig,
        core_id: int = 0,
        runtime: ModelRuntime | None = None,
    ) -> None:
        self.model_path = str(model_path)
        self.input_size = int(config.input_size)
        self.obj_thresh = float(config.obj_thresh)
        self.nms_thresh = float(config.nms_thresh)
        self._runtime = runtime or RKNNModelRunner(model_path=self.model_path, core_id=core_id)

    def detect(self, frame_rgb: np.ndarray) -> list[DetectedFace]:
        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            raise ValueError("face detector expects HxWx3 RGB frames")

        letterboxed = letterbox_rgb(np.ascontiguousarray(frame_rgb), target_size=self.input_size)
        outputs = self._runtime.infer(letterboxed.image[None, ...], data_format="nhwc")
        boxes, _classes, scores = yolov8_post_process(
            outputs,
            obj_thresh=self.obj_thresh,
            nms_thresh=self.nms_thresh,
            img_size=self.input_size,
        )
        if boxes is None or scores is None or len(boxes) == 0:
            return []

        src_h, src_w = frame_rgb.shape[:2]
        faces: list[DetectedFace] = []
        for box, score in zip(boxes, scores):
            mapped = (
                (float(box[0]) - letterboxed.pad_x) / letterboxed.scale,
                (float(box[1]) - letterboxed.pad_y) / letterboxed.scale,
                (float(box[2]) - letterboxed.pad_x) / letterboxed.scale,
                (float(box[3]) - letterboxed.pad_y) / letterboxed.scale,
            )
            clipped = clip_box(mapped, src_w, src_h)
            if clipped is None:
                continue
            faces.append(DetectedFace(bbox=clipped, score=float(score)))
        return faces

    def close(self) -> None:
        self._runtime.release()
