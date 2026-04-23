"""人脸特征提取适配器。"""

from __future__ import annotations

import logging

import numpy as np

from adapters.common import crop_with_margin, prepare_rgb_model_input
from adapters.rknn_runtime import ModelRuntime, RKNNModelRunner
from services.config import RecognizerConfig


LOGGER = logging.getLogger(__name__)


def normalize_embedding(embedding: np.ndarray) -> np.ndarray:
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-6:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)


class FaceEmbeddingAdapter:
    """封装人脸裁剪、预处理和 embedding 提取。"""

    def __init__(
        self,
        model_path: str,
        config: RecognizerConfig,
        core_id: int = 0,
        runtime: ModelRuntime | None = None,
    ) -> None:
        self.model_path = str(model_path)
        self.input_size = int(config.input_size)
        self.crop_margin = float(config.crop_margin)
        self.unknown_label = str(config.unknown_label)
        self.preprocess = str(config.preprocess or "raw_uint8").strip().lower()
        self.input_color = str(config.input_color or "rgb").strip().lower()
        self.input_scale = float(config.input_scale)
        self.mean = np.asarray(config.mean, dtype=np.float32).reshape(-1)
        self.std = np.asarray(config.std, dtype=np.float32).reshape(-1)
        if self.mean.size != 3:
            raise ValueError("recognizer.mean 必须是 3 个值")
        if self.std.size != 3:
            raise ValueError("recognizer.std 必须是 3 个值")
        self.std = np.where(np.abs(self.std) < 1e-6, 1.0, self.std)
        self._runtime = runtime or RKNNModelRunner(model_path=self.model_path, core_id=core_id)

    def crop_face(self, frame_rgb: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray | None:
        return crop_with_margin(frame_rgb, bbox, margin_ratio=self.crop_margin)

    def extract_from_crop(self, face_rgb: np.ndarray) -> np.ndarray:
        model_input = prepare_rgb_model_input(
            image_rgb=face_rgb,
            target_size=self.input_size,
            input_color=self.input_color,
            preprocess_mode=self.preprocess,
            input_scale=self.input_scale,
            mean=self.mean,
            std=self.std,
            presets={
                "divide_255": {"dtype": "float32", "scale": 1.0 / 255.0},
                "center_1275": {
                    "dtype": "float32",
                    "scale": 1.0,
                    "mean": [127.5, 127.5, 127.5],
                    "std": [127.5, 127.5, 127.5],
                },
                "raw_float32": {"dtype": "float32", "scale": 1.0},
                "raw_uint8": {"dtype": "uint8"},
            },
            fallback_mode="raw_uint8",
            logger=LOGGER,
            model_name="recognizer",
        )
        outputs = self._runtime.infer(model_input[None, ...], data_format="nhwc")
        embedding = np.asarray(outputs[0]).reshape(-1).astype(np.float32)
        return normalize_embedding(embedding)

    def extract(self, frame_rgb: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray | None:
        crop = self.crop_face(frame_rgb, bbox)
        if crop is None:
            return None
        return self.extract_from_crop(crop)

    def close(self) -> None:
        self._runtime.release()
