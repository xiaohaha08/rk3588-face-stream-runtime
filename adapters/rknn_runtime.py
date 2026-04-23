"""Thin RKNNLite runtime wrapper with lazy imports."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np


class ModelRuntime(Protocol):
    def infer(self, input_tensor: np.ndarray, data_format: str = "nhwc") -> list[np.ndarray]:
        """Run one inference pass."""

    def release(self) -> None:
        """Release runtime resources."""


class RKNNRuntimeError(RuntimeError):
    """Raised when RKNNLite runtime is unavailable or fails to initialize."""


def _import_rknn_lite():
    try:
        from rknnlite.api import RKNNLite
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on target device env
        raise RKNNRuntimeError(
            "rknnlite is not installed. Install RKNNLite on RK3588 before using face_detect_quality mode."
        ) from exc
    return RKNNLite


class RKNNModelRunner:
    """One RKNN model instance bound to one worker/core."""

    def __init__(self, model_path: str, core_id: int = 0) -> None:
        resolved = Path(model_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"missing RKNN model: {resolved}")

        RKNNLite = _import_rknn_lite()
        runtime = RKNNLite()
        if runtime.load_rknn(str(resolved)) != 0:
            raise RKNNRuntimeError(f"failed to load RKNN model: {resolved}")

        core_index = int(core_id) % 3
        core_mask = {
            0: RKNNLite.NPU_CORE_0,
            1: RKNNLite.NPU_CORE_1,
            2: RKNNLite.NPU_CORE_2,
        }.get(core_index, RKNNLite.NPU_CORE_0)
        try:
            init_ret = runtime.init_runtime(core_mask=core_mask)
        except Exception as exc:  # pragma: no cover - depends on target device env
            message = str(exc)
            if "librknnrt.so" in message or "dynamic library" in message.lower():
                raise RKNNRuntimeError(
                    "failed to init RKNN runtime because librknnrt.so is missing. "
                    "Set RKNNRT_LIB_PATH or install librknnrt.so on RK3588 first."
                ) from None
            raise RKNNRuntimeError(
                f"failed to init RKNN runtime on core {core_index}: {resolved}: {message}"
            ) from None
        if init_ret != 0:
            raise RKNNRuntimeError(f"failed to init RKNN runtime on core {core_index}: {resolved}")

        self.model_path = str(resolved)
        self.core_id = core_index
        self._runtime = runtime

    def infer(self, input_tensor: np.ndarray, data_format: str = "nhwc") -> list[np.ndarray]:
        outputs = self._runtime.inference(inputs=[input_tensor], data_format=[data_format])
        return list(outputs)

    def release(self) -> None:
        self._runtime.release()
