"""Input source contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np


class BaseFrameSource(ABC):
    """Abstract single-source frame reader."""

    def open(self) -> None:
        """Acquire resources before reading."""

    @abstractmethod
    def read(self) -> np.ndarray | None:
        """Return the next frame or ``None`` when the source is exhausted."""

    def close(self) -> None:
        """Release resources."""

    def snapshot_info(self) -> dict[str, Any]:
        """Return best-effort metadata about the opened input source."""

        return {}
