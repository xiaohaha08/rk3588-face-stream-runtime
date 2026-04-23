"""Face quality classifier adapter."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from adapters.common import CLASSIFIER_PREPROCESS_PRESETS, clip_box, prepare_rgb_model_input, softmax
from adapters.rknn_runtime import ModelRuntime, RKNNModelRunner
from services.config import ClassifierConfig


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualityClassification:
    label: str
    score: float


class FaceQualityAdapter:
    """Classifier wrapper that scores one detected face crop."""

    def __init__(
        self,
        model_path: str,
        config: ClassifierConfig,
        core_id: int = 0,
        runtime: ModelRuntime | None = None,
    ) -> None:
        self.model_path = str(model_path)
        self.input_size = int(config.input_size)
        self.crop_scale = float(config.crop_scale)
        self.center_shift_y = float(config.center_shift_y)
        self.use_face_crop_direct_resize = bool(config.use_face_crop_direct_resize)
        self.use_softmax = bool(config.softmax)
        self.preprocess = str(config.preprocess or "raw_uint8").strip().lower()
        self.input_color = str(config.input_color or "rgb").strip().lower()
        self.input_scale = float(config.input_scale)
        self.labels = tuple(config.labels)
        self.mean = np.asarray(config.mean, dtype=np.float32).reshape(-1)
        self.std = np.asarray(config.std, dtype=np.float32).reshape(-1)
        if self.mean.size != 3:
            raise ValueError("classifier.mean must have exactly 3 values")
        if self.std.size != 3:
            raise ValueError("classifier.std must have exactly 3 values")
        self.std = np.where(np.abs(self.std) < 1e-6, 1.0, self.std)
        self._runtime = runtime or RKNNModelRunner(model_path=self.model_path, core_id=core_id)

    def crop_face(self, frame_rgb: np.ndarray, bbox: Sequence[int]) -> np.ndarray | None:
        if self.use_face_crop_direct_resize:
            crop_box = clip_box(bbox, frame_rgb.shape[1], frame_rgb.shape[0])
        else:
            x1, y1, x2, y2 = [float(v) for v in bbox]
            width = max(2.0, x2 - x1)
            height = max(2.0, y2 - y1)
            side = max(width, height) * max(1.0, self.crop_scale)
            center_x = (x1 + x2) * 0.5
            center_y = (y1 + y2) * 0.5 + (height * self.center_shift_y)
            half = side * 0.5
            crop_box = clip_box(
                (
                    center_x - half,
                    center_y - half,
                    center_x + half,
                    center_y + half,
                ),
                frame_rgb.shape[1],
                frame_rgb.shape[0],
            )
        if crop_box is None:
            return None
        cx1, cy1, cx2, cy2 = crop_box
        crop = frame_rgb[cy1:cy2, cx1:cx2]
        if crop.size == 0:
            return None
        return crop

    def classify_crop(self, face_rgb: np.ndarray) -> QualityClassification:
        model_input = prepare_rgb_model_input(
            image_rgb=face_rgb,
            target_size=self.input_size,
            input_color=self.input_color,
            preprocess_mode=self.preprocess,
            input_scale=self.input_scale,
            mean=self.mean,
            std=self.std,
            presets=CLASSIFIER_PREPROCESS_PRESETS,
            fallback_mode="raw_uint8",
            logger=LOGGER,
            model_name="classifier",
        )
        outputs = self._runtime.infer(model_input[None, ...], data_format="nhwc")
        logits = np.asarray(outputs[0]).reshape(-1).astype(np.float32)
        if logits.size == 0:
            return QualityClassification(label="", score=0.0)
        if self.use_softmax:
            probs = softmax(logits)
            index = int(np.argmax(probs))
            score = float(probs[index])
        else:
            index = int(np.argmax(logits))
            score = float(logits[index])
        label = self.labels[index] if 0 <= index < len(self.labels) else f"class_{index}"
        return QualityClassification(label=label, score=score)

    def classify(self, frame_rgb: np.ndarray, bbox: Sequence[int]) -> QualityClassification:
        crop = self.crop_face(frame_rgb, bbox)
        if crop is None:
            return QualityClassification(label="", score=0.0)
        return self.classify_crop(crop)

    def close(self) -> None:
        self._runtime.release()
