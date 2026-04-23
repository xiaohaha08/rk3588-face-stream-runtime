"""Helpers for composing result enrichers."""

from __future__ import annotations

from pipelines.types import InferenceResult


class LabelFilteredResultEnricher:
    """Apply an enricher only to detections whose labels match a given allow-list."""

    def __init__(self, delegate, allowed_labels: set[str] | tuple[str, ...] | list[str]) -> None:
        self.delegate = delegate
        self.allowed_labels = {str(item).strip().lower() for item in allowed_labels if str(item).strip()}

    @property
    def recognizer_enabled(self) -> bool:
        return bool(getattr(self.delegate, "recognizer_enabled", False))

    @property
    def gallery_identity_count(self) -> int:
        return int(getattr(self.delegate, "gallery_identity_count", 0))

    def enrich(self, frame, result: InferenceResult | None):
        if result is None or not result.detections:
            return result

        selected_indices = [
            index
            for index, detection in enumerate(result.detections)
            if str(detection.label or "").strip().lower() in self.allowed_labels
        ]
        if not selected_indices:
            return result

        filtered = InferenceResult(
            source_id=result.source_id,
            frame_id=result.frame_id,
            status=result.status,
            detections=[result.detections[index] for index in selected_indices],
            worker_id=result.worker_id,
            started_monotonic=result.started_monotonic,
            completed_monotonic=result.completed_monotonic,
            process_ms=result.process_ms,
            error=result.error,
            timing_ms=dict(result.timing_ms),
        )
        enriched = self.delegate.enrich(frame, filtered)
        if enriched is None:
            return result

        merged = list(result.detections)
        for index, detection in zip(selected_indices, enriched.detections):
            merged[index] = detection
        result.detections = merged
        return result

    def close(self) -> None:
        close_fn = getattr(self.delegate, "close", None)
        if callable(close_fn):
            close_fn()
