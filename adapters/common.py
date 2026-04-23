"""Shared image helpers for model adapters."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import cv2
import numpy as np


CLASSIFIER_PREPROCESS_PRESETS: dict[str, dict[str, object]] = {
    "rk_caffe": {
        "dtype": "float32",
        "scale": 1.0,
        "mean": [123.675, 116.28, 103.53],
        "std": [58.395, 57.12, 57.375],
    },
    "torch_imagenet": {
        "dtype": "float32",
        "scale": 1.0 / 255.0,
        "mean": [0.485, 0.456, 0.406],
        "std": [0.229, 0.224, 0.225],
    },
    "raw_float32": {
        "dtype": "float32",
        "scale": 1.0,
    },
    "raw_uint8": {
        "dtype": "uint8",
    },
    "legacy_mixed": {
        "dtype": "float32",
        "scale": 1.0,
        "mean": [0.485, 0.456, 0.406],
        "std": [58.395, 57.12, 57.375],
    },
}


@dataclass(frozen=True)
class LetterboxResult:
    image: np.ndarray
    scale: float
    pad_x: int
    pad_y: int


def softmax(values: np.ndarray) -> np.ndarray:
    logits = np.asarray(values, dtype=np.float32).reshape(-1)
    if logits.size == 0:
        return logits
    logits = logits - np.max(logits)
    exps = np.exp(logits)
    denom = np.sum(exps)
    if float(denom) <= 1e-12:
        return np.zeros_like(logits)
    return exps / denom


def clip_box(box: Sequence[float], width: int, height: int) -> tuple[int, int, int, int] | None:
    if width <= 1 or height <= 1:
        return None
    x1 = max(0, min(int(math.floor(float(box[0]))), width - 1))
    y1 = max(0, min(int(math.floor(float(box[1]))), height - 1))
    x2 = max(0, min(int(math.ceil(float(box[2]))), width - 1))
    y2 = max(0, min(int(math.ceil(float(box[3]))), height - 1))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return x1, y1, x2, y2


def crop_with_margin(frame_rgb: np.ndarray, bbox: Sequence[int], margin_ratio: float) -> np.ndarray | None:
    x1, y1, x2, y2 = [int(v) for v in bbox]
    width = max(2, x2 - x1)
    height = max(2, y2 - y1)
    expand_x = int(round(width * float(margin_ratio)))
    expand_y = int(round(height * float(margin_ratio)))
    crop_box = clip_box(
        (x1 - expand_x, y1 - expand_y, x2 + expand_x, y2 + expand_y),
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


def letterbox_rgb(
    rgb: np.ndarray,
    target_size: int,
    pad_color: tuple[int, int, int] = (0, 0, 0),
) -> LetterboxResult:
    src_h, src_w = rgb.shape[:2]
    if src_h <= 0 or src_w <= 0:
        raise ValueError("invalid image shape for letterbox")

    ratio = min(float(target_size) / float(src_w), float(target_size) / float(src_h))
    resized_w = max(1, int(round(src_w * ratio)))
    resized_h = max(1, int(round(src_h * ratio)))
    resized = cv2.resize(rgb, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)

    dw = float(target_size - resized_w)
    dh = float(target_size - resized_h)
    left = int(round(dw / 2.0 - 0.1))
    right = int(round(dw / 2.0 + 0.1))
    top = int(round(dh / 2.0 - 0.1))
    bottom = int(round(dh / 2.0 + 0.1))
    canvas = cv2.copyMakeBorder(
        resized,
        top,
        bottom,
        left,
        right,
        cv2.BORDER_CONSTANT,
        value=pad_color,
    )
    if canvas.shape[0] != target_size or canvas.shape[1] != target_size:
        canvas = cv2.resize(canvas, (target_size, target_size), interpolation=cv2.INTER_LINEAR)

    return LetterboxResult(
        image=np.ascontiguousarray(canvas),
        scale=ratio,
        pad_x=left,
        pad_y=top,
    )


def prepare_rgb_model_input(
    image_rgb: np.ndarray,
    target_size: int,
    input_color: str,
    preprocess_mode: str,
    input_scale: float,
    mean: np.ndarray,
    std: np.ndarray,
    presets: Mapping[str, Mapping[str, object]],
    fallback_mode: str,
    logger: logging.Logger,
    model_name: str,
) -> np.ndarray:
    prepared = image_rgb
    if prepared.shape[0] != target_size or prepared.shape[1] != target_size:
        prepared = cv2.resize(prepared, (target_size, target_size), interpolation=cv2.INTER_LINEAR)

    color_mode = str(input_color or "rgb").strip().lower()
    if color_mode == "bgr":
        prepared = cv2.cvtColor(prepared, cv2.COLOR_RGB2BGR)
    elif color_mode != "rgb":
        logger.warning("unknown %s input_color=%s, fallback to rgb", model_name, input_color)

    mode = str(preprocess_mode or "").strip().lower()
    if mode == "custom":
        model_input = prepared.astype(np.float32)
        if not math.isclose(float(input_scale), 1.0, rel_tol=0.0, abs_tol=1e-9):
            model_input *= float(input_scale)
        return (model_input - mean.reshape(1, 1, 3)) / std.reshape(1, 1, 3)

    preset = presets.get(mode)
    if preset is None:
        logger.warning("unknown %s preprocess=%s, fallback to %s", model_name, mode, fallback_mode)
        preset = presets[fallback_mode]

    if preset.get("dtype") == "uint8":
        return prepared.astype(np.uint8, copy=False)

    model_input = prepared.astype(np.float32)
    scale = float(preset.get("scale", 1.0) or 1.0)
    if not math.isclose(scale, 1.0, rel_tol=0.0, abs_tol=1e-9):
        model_input *= scale
    preset_mean = preset.get("mean")
    preset_std = preset.get("std")
    if preset_mean is None or preset_std is None:
        return model_input
    mean_arr = np.asarray(preset_mean, dtype=np.float32).reshape(1, 1, 3)
    std_arr = np.asarray(preset_std, dtype=np.float32).reshape(1, 1, 3)
    std_arr = np.where(np.abs(std_arr) < 1e-6, 1.0, std_arr)
    return (model_input - mean_arr) / std_arr
