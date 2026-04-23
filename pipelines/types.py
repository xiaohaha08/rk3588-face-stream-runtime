"""Shared data structures for the stage-1 runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np


BBox = Tuple[int, int, int, int]


class WorkerStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    bbox: BBox
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CapturedFrame:
    source_id: str
    frame_id: int
    capture_monotonic: float
    capture_wall_time: float
    frame: np.ndarray
    timing_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerTask:
    source_id: str
    frame_id: int
    capture_monotonic: float
    capture_wall_time: float
    frame: np.ndarray
    deadline_monotonic: float
    timing_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class InferenceResult:
    source_id: str
    frame_id: int
    status: WorkerStatus
    detections: List[Detection] = field(default_factory=list)
    worker_id: int = -1
    started_monotonic: float = 0.0
    completed_monotonic: float = 0.0
    process_ms: float = 0.0
    error: str = ""
    timing_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ReorderDecision:
    frame: CapturedFrame
    result: Optional[InferenceResult]
    reason: str


@dataclass(frozen=True)
class PushPacket:
    source_id: str
    frame_id: int
    capture_monotonic: float
    frame: np.ndarray
    reason: str
    status: WorkerStatus
    ready_monotonic: float = 0.0
    target_output_fps: float = 0.0
    timing_ms: dict[str, float] = field(default_factory=dict)
