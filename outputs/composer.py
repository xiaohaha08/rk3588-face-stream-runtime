"""Frame composer for stage-1 validation."""

from __future__ import annotations

from typing import Iterable

import numpy as np

from pipelines.types import InferenceResult, WorkerStatus


def _optional_cv2():
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    return cv2


def _clamp_box(x1: int, y1: int, x2: int, y2: int, width: int, height: int) -> tuple[int, int, int, int]:
    return (
        max(0, min(width - 1, x1)),
        max(0, min(height - 1, y1)),
        max(0, min(width - 1, x2)),
        max(0, min(height - 1, y2)),
    )


def _draw_rect(frame: np.ndarray, bbox: tuple[int, int, int, int], color: tuple[int, int, int], thickness: int = 2) -> None:
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = _clamp_box(*bbox, width, height)
    if x2 <= x1 or y2 <= y1:
        return
    t = max(1, thickness)
    frame[y1:y1 + t, x1:x2] = color
    frame[max(y1, y2 - t):y2, x1:x2] = color
    frame[y1:y2, x1:x1 + t] = color
    frame[y1:y2, max(x1, x2 - t):x2] = color


def _draw_text_lines(
    frame: np.ndarray,
    lines: Iterable[str],
    origin: tuple[int, int],
    color: tuple[int, int, int],
    background: tuple[int, int, int] | None = None,
) -> None:
    cv2 = _optional_cv2()
    if cv2 is None:
        return
    height, width = frame.shape[:2]
    x, y = origin
    for line in lines:
        text = str(line or "").strip()
        if not text:
            continue
        if background is not None:
            (text_w, text_h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.48, 1)
            pad_x = 4
            pad_y = 3
            left = max(0, int(x - pad_x))
            right = min(width - 1, int(x + text_w + pad_x))
            top = max(0, int(y - text_h - baseline - pad_y))
            bottom = min(height - 1, int(y + pad_y))
            cv2.rectangle(frame, (left, top), (right, bottom), background, thickness=-1)
        cv2.putText(
            frame,
            text,
            (int(x), int(y)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            cv2.LINE_AA,
        )
        y += 18


def _has_text_lines(lines: Iterable[str]) -> bool:
    return any(bool(str(line or "").strip()) for line in lines)


def _is_target_face(attributes: dict[str, object]) -> bool:
    identity = str(attributes.get("identity", "") or "").strip()
    if not identity or identity.lower() == "unknown":
        return False
    try:
        similarity = float(attributes.get("similarity", 0.0) or 0.0)
    except (TypeError, ValueError):
        similarity = 0.0
    return similarity > 0.0 and bool(attributes.get("identity_enabled", False))


def _is_recognized_face(attributes: dict[str, object]) -> bool:
    identity = str(attributes.get("identity", "") or "").strip()
    if not identity or identity.lower() == "unknown":
        return False
    try:
        similarity = float(attributes.get("similarity", 0.0) or 0.0)
    except (TypeError, ValueError):
        similarity = 0.0
    return similarity > 0.0


class FrameComposer:
    """Compose structured results onto output frames."""

    def __init__(self, annotate_failures: bool = True) -> None:
        self.annotate_failures = bool(annotate_failures)

    def compose(self, frame: np.ndarray, result: InferenceResult | None, reason: str) -> np.ndarray:
        composed: np.ndarray | None = None
        cv2_available = _optional_cv2() is not None

        def writable_frame() -> np.ndarray:
            nonlocal composed
            if composed is None:
                composed = np.array(frame, copy=True)
            return composed

        def draw_text_if_visible(
            lines: Iterable[str],
            origin: tuple[int, int],
            color: tuple[int, int, int],
            background: tuple[int, int, int] | None = None,
        ) -> None:
            if not cv2_available or not _has_text_lines(lines):
                return
            _draw_text_lines(writable_frame(), lines, origin=origin, color=color, background=background)

        if result is not None and result.status == WorkerStatus.SUCCESS:
            for detection in result.detections:
                attrs = dict(detection.attributes)
                detection_label = str(detection.label or "").strip().lower()
                if detection_label != "face":
                    continue

                identity = str(attrs.get("identity", "") or "")
                is_target = _is_target_face(attrs)
                is_recognized = _is_recognized_face(attrs)
                box_color = (255, 0, 0) if is_target else (0, 255, 0)
                shown_identity = identity if is_recognized else "unknown"

                _draw_rect(writable_frame(), detection.bbox, box_color, thickness=3)
                x1, y1, _x2, _y2 = detection.bbox
                draw_text_if_visible(
                    [shown_identity],
                    origin=(max(4, x1), max(18, y1 - 6)),
                    color=(255, 255, 255),
                    background=box_color,
                )
        return composed if composed is not None else frame
