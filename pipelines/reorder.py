"""Weak-order output reordering."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from pipelines.types import CapturedFrame, InferenceResult, ReorderDecision


class WeakOrderReorderer:
    """Prefer frame_id order, but skip frames that wait too long."""

    def __init__(self, source_id: str, max_wait_ms: int) -> None:
        self.source_id = source_id
        self.max_wait_ms = int(max_wait_ms)
        self.next_frame_id = 0
        self._results: Dict[int, InferenceResult] = {}

    def add_result(self, result: InferenceResult) -> bool:
        if result.frame_id < self.next_frame_id:
            return False
        self._results[result.frame_id] = result
        return True

    def pending_results(self) -> int:
        return len(self._results)

    def drain_ready(
        self,
        latest_frame_id: int,
        now_monotonic: float,
        frame_get: Callable[[int], Optional[CapturedFrame]],
        frame_pop: Callable[[int], Optional[CapturedFrame]],
        metrics,
    ) -> List[ReorderDecision]:
        ready: List[ReorderDecision] = []
        while self.next_frame_id <= latest_frame_id:
            frame = frame_get(self.next_frame_id)
            if frame is None:
                metrics.record_reorder_skip()
                self.next_frame_id += 1
                continue

            result = self._results.get(self.next_frame_id)
            if result is not None:
                self._results.pop(self.next_frame_id, None)
                ready.append(
                    ReorderDecision(
                        frame=frame_pop(self.next_frame_id) or frame,
                        result=result,
                        reason="matched" if result.status.value == "success" else result.status.value,
                    )
                )
                self.next_frame_id += 1
                continue

            wait_ms = (now_monotonic - frame.capture_monotonic) * 1000.0
            if wait_ms < self.max_wait_ms:
                metrics.record_reorder_wait()
                break

            ready.append(
                ReorderDecision(
                    frame=frame_pop(self.next_frame_id) or frame,
                    result=None,
                    reason="timeout_skip",
                )
            )
            metrics.record_reorder_skip()
            self.next_frame_id += 1
        return ready
