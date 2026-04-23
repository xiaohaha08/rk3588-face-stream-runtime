"""Runtime manager for lifecycle and control-plane state."""

from __future__ import annotations

from dataclasses import replace
import logging
import threading
from pathlib import Path
from typing import Callable

from services.config import (
    disable_face_recognition,
    infer_processing_mode,
    load_runtime_config,
    RuntimeConfig,
)
from services.bootstrap import build_alarm_store
from services.event_bus import EventBus
from services.managed_runtime import build_managed_runtime


LOGGER = logging.getLogger(__name__)


class RuntimeManager:
    """Manage one runtime instance and expose control-plane state."""

    def __init__(
        self,
        config_path: str,
        event_bus: EventBus | None = None,
        config_loader: Callable[[str], RuntimeConfig] = load_runtime_config,
        runtime_factory: Callable[[RuntimeConfig, EventBus | None], object] | None = None,
    ) -> None:
        self.config_path = str(Path(config_path).expanduser().resolve())
        self.event_bus = event_bus or EventBus()
        self.config_loader = config_loader
        self.runtime_factory = runtime_factory or (lambda config, event_bus: build_managed_runtime(config, event_bus=event_bus))
        self._lock = threading.RLock()
        self._state = "stopped"
        self._last_error = ""
        self._runtime = None
        self._watcher: threading.Thread | None = None
        self._current_config: RuntimeConfig | None = None
        self._model_status: dict[str, dict[str, object]] = {}
        self._alarm_enabled_override: bool | None = None
        self._face_recognition_enabled_override: bool | None = None
        self._alarm_store = None
        self._runtime_overrides: dict[str, object] = {}

    def start(self, config_path: str | None = None, runtime_overrides: dict[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            if config_path:
                self.config_path = str(Path(config_path).expanduser().resolve())
            if runtime_overrides is not None:
                self._runtime_overrides = dict(runtime_overrides)
            if self._runtime is not None and self._state in {"starting", "running"}:
                return self.snapshot_state()

            self._state = "starting"
            self._last_error = ""
            config = self._load_effective_config(self.config_path)
            self._current_config = config
            self._model_status = self._build_model_status(config=config, state="loading")
            self._publish("runtime.state", config.source_id, {"state": self._state, "config_path": self.config_path})
            self._publish("runtime.mode", config.source_id, {"processing_mode": infer_processing_mode(config)})
            self._publish("models.state", config.source_id, {"models": self._model_status})
            try:
                runtime = self.runtime_factory(config, self.event_bus)
            except Exception as exc:
                self._state = "error"
                self._last_error = str(exc)
                self._model_status = self._build_model_status(config=config, state="failed", error=str(exc))
                self._publish("runtime.error", config.source_id, {"component": "bootstrap", "error": str(exc)})
                self._publish("models.state", config.source_id, {"models": self._model_status})
                raise

            self._runtime = runtime
            self._alarm_store = getattr(runtime, "alarm_store", None)
            try:
                runtime.start()
            except Exception as exc:
                try:
                    runtime.stop()
                except Exception:
                    LOGGER.exception("runtime stop after start failure failed")
                self._runtime = None
                self._state = "error"
                self._last_error = str(exc)
                self._alarm_store = None
                self._model_status = self._build_model_status(config=config, state="failed", runtime=runtime, error=str(exc))
                self._publish("runtime.error", config.source_id, {"component": "runtime.start", "error": str(exc)})
                self._publish("models.state", config.source_id, {"models": self._model_status})
                raise

            self._state = "running"
            self._model_status = self._build_model_status(config=config, state="loaded", runtime=runtime)
            self._watcher = threading.Thread(target=self._watch_runtime, name="RuntimeWatcher", daemon=True)
            self._watcher.start()
            self._publish("runtime.state", config.source_id, {"state": self._state, "config_path": self.config_path})
            self._publish("runtime.mode", config.source_id, {"processing_mode": infer_processing_mode(config)})
            self._publish("models.state", config.source_id, {"models": self._model_status})
            return self.snapshot_state()

    def stop(self) -> dict[str, object]:
        with self._lock:
            runtime = self._runtime
            config = self._current_config
            if runtime is None:
                self._state = "stopped"
                return self.snapshot_state()
            self._state = "stopping"
            if config is not None:
                self._publish("runtime.state", config.source_id, {"state": self._state, "config_path": self.config_path})

        runtime.stop()
        watcher = self._watcher
        if watcher is not None:
            watcher.join(timeout=5.0)

        with self._lock:
            self._runtime = None
            self._watcher = None
            self._state = "stopped"
            if config is not None:
                self._publish("runtime.state", config.source_id, {"state": self._state, "config_path": self.config_path})
            return self.snapshot_state()

    def snapshot_state(self) -> dict[str, object]:
        with self._lock:
            config = self._current_config
            metrics = self._runtime.snapshot_metrics() if self._runtime is not None else {"sources": {}}
            input_info = (
                self._runtime.snapshot_input_info()
                if self._runtime is not None and hasattr(self._runtime, "snapshot_input_info")
                else {}
            )
            output_info = (
                self._runtime.snapshot_output_info()
                if self._runtime is not None and hasattr(self._runtime, "snapshot_output_info")
                else {}
            )
            services = (
                self._runtime.snapshot_services()
                if self._runtime is not None and hasattr(self._runtime, "snapshot_services")
                else {}
            )
            if not services and config is not None:
                services = {
                    "mediamtx": {
                        "enabled": bool(config.mediamtx.enabled),
                        "state": "disabled" if not config.mediamtx.enabled else "not_started",
                        "host": config.mediamtx.host,
                        "port": config.mediamtx.port,
                        "auto_start": bool(config.mediamtx.auto_start),
                        "owned": False,
                        "binary_path": config.mediamtx.binary_path,
                        "config_path": config.mediamtx.config_path,
                    }
                }
            return {
                "state": self._state,
                "config_path": self.config_path,
                "source_id": config.source_id if config is not None else "",
                "worker_mode": config.pipeline.worker_mode if config is not None else "",
                "processing_mode": infer_processing_mode(config),
                "last_error": self._last_error,
                "models": self._model_status,
                "services": services,
                "metrics": metrics,
                "input_info": input_info,
                "output_info": output_info,
            }

    def snapshot_metrics(self) -> dict[str, object]:
        with self._lock:
            if self._runtime is None:
                return {"sources": {}}
            return self._runtime.snapshot_metrics()

    def current_config(self) -> RuntimeConfig | None:
        with self._lock:
            return self._current_config

    def preview_config(self) -> RuntimeConfig | None:
        with self._lock:
            config = self._current_config
            config_path = self.config_path
        if config is not None:
            return config
        try:
            return self._load_effective_config(config_path)
        except Exception:
            LOGGER.exception("failed to preview runtime config: %s", config_path)
            return None

    def recent_events(self, limit: int = 100, topic: str | None = None) -> list[dict[str, object]]:
        return self.event_bus.recent(limit=limit, topic=topic)

    def recent_alarms(self, limit: int = 100, active_only: bool = False) -> list[dict[str, object]]:
        with self._lock:
            runtime = self._runtime
            alarm_store = self._alarm_store
        if runtime is not None and hasattr(runtime, "snapshot_alarms"):
            return runtime.snapshot_alarms(limit=limit, active_only=active_only)
        if alarm_store is None:
            alarm_store = self._ensure_alarm_store()
        if alarm_store is not None and hasattr(alarm_store, "recent"):
            return alarm_store.recent(limit=limit, active_only=active_only)
        return []

    def get_alarm(self, alarm_id: int) -> dict[str, object] | None:
        with self._lock:
            runtime = self._runtime
            alarm_store = self._alarm_store
        if runtime is not None and hasattr(runtime, "get_alarm"):
            value = runtime.get_alarm(alarm_id)
            if value is not None:
                return value
        if alarm_store is None:
            alarm_store = self._ensure_alarm_store()
        if alarm_store is not None and hasattr(alarm_store, "get"):
            return alarm_store.get(alarm_id)
        return None

    def resolve_alarm_image(self, relative_path: str):
        with self._lock:
            runtime = self._runtime
            alarm_store = self._alarm_store
        if runtime is not None and hasattr(runtime, "resolve_alarm_image"):
            value = runtime.resolve_alarm_image(relative_path)
            if value is not None:
                return value
        if alarm_store is None:
            alarm_store = self._ensure_alarm_store()
        if alarm_store is not None and hasattr(alarm_store, "resolve_image_path"):
            return alarm_store.resolve_image_path(relative_path)
        return None

    def delete_alarm(self, alarm_id: int) -> dict[str, object]:
        store = self._ensure_alarm_store()
        if store is None or not hasattr(store, "delete"):
            raise KeyError(f"alarm not found: {alarm_id}")
        return store.delete(alarm_id)

    def delete_all_alarms(self) -> dict[str, object]:
        store = self._ensure_alarm_store()
        if store is None or not hasattr(store, "delete_all"):
            return {"deleted_alarms": 0, "deleted_images": 0}
        return store.delete_all()

    def set_alarm_enabled(self, enabled: bool) -> dict[str, object]:
        normalized = bool(enabled)
        with self._lock:
            self._alarm_enabled_override = normalized
            runtime = self._runtime
            runtime_state = self._state
            config_path = self.config_path

        if runtime is not None and runtime_state in {"starting", "running"} and hasattr(runtime, "set_alarm_enabled"):
            actual = bool(runtime.set_alarm_enabled(normalized))
            config = self._load_effective_config(config_path)
            with self._lock:
                self._alarm_enabled_override = actual
                self._current_config = config
            return self.snapshot_state()

        store = self._ensure_alarm_store()
        if store is not None and hasattr(store, "set_enabled"):
            actual = bool(store.set_enabled(normalized))
            with self._lock:
                self._alarm_enabled_override = actual

        config = self._load_effective_config(config_path)
        with self._lock:
            self._current_config = config
            self._model_status = self._build_model_status(config=config, state="stopped")
        return self.snapshot_state()

    def set_face_recognition_enabled(self, enabled: bool) -> dict[str, object]:
        normalized = bool(enabled)
        with self._lock:
            self._face_recognition_enabled_override = normalized
            runtime = self._runtime
            runtime_state = self._state
            config_path = self.config_path

        if runtime is not None and runtime_state in {"starting", "running"} and hasattr(runtime, "set_face_recognition_enabled"):
            actual = bool(runtime.set_face_recognition_enabled(normalized))
            config = self._load_effective_config(config_path)
            with self._lock:
                self._face_recognition_enabled_override = actual
                self._current_config = config
                self._model_status = self._build_model_status(config=config, state="loaded", runtime=runtime)
            return self.snapshot_state()

        config = self._load_effective_config(config_path)
        with self._lock:
            self._current_config = config
            self._model_status = self._build_model_status(config=config, state="stopped")
        return self.snapshot_state()

    def set_face_identity_enabled(self, identity: str, enabled: bool) -> bool:
        name = str(identity or "").strip()
        if not name:
            return False
        with self._lock:
            runtime = self._runtime
            runtime_state = self._state
            alarm_store = self._alarm_store
        updated = False
        if runtime is not None and runtime_state in {"starting", "running"} and hasattr(runtime, "set_face_identity_enabled"):
            updated = bool(runtime.set_face_identity_enabled(name, bool(enabled)))
        if alarm_store is None:
            alarm_store = self._ensure_alarm_store()
        if alarm_store is not None and hasattr(alarm_store, "set_identity_enabled"):
            alarm_store.set_identity_enabled(name, bool(enabled))
        return updated

    def _publish(self, topic: str, source_id: str, payload: dict[str, object]) -> None:
        self.event_bus.publish(topic=topic, source_id=source_id, payload=payload)

    def _watch_runtime(self) -> None:
        with self._lock:
            runtime = self._runtime
            config = self._current_config
        if runtime is None or config is None:
            return

        try:
            runtime.wait(timeout=None)
            with self._lock:
                if self._runtime is runtime:
                    self._runtime = None
                    self._watcher = None
                    if self._state != "stopped":
                        self._state = "stopped"
                        self._publish("runtime.state", config.source_id, {"state": self._state, "config_path": self.config_path})
        except Exception as exc:
            LOGGER.exception("runtime watcher failed")
            with self._lock:
                self._runtime = None
                self._watcher = None
                self._state = "error"
                self._last_error = str(exc)
                self._publish("runtime.error", config.source_id, {"component": "watcher", "error": str(exc)})

    def _build_model_status(
        self,
        config: RuntimeConfig,
        state: str,
        runtime=None,
        error: str = "",
    ) -> dict[str, dict[str, object]]:
        recognizer_enabled = False
        gallery_identities = 0
        enricher = getattr(runtime, "result_enricher", None)
        if enricher is not None:
            recognizer_enabled = bool(getattr(enricher, "recognizer_enabled", False))
            gallery_identities = int(getattr(enricher, "gallery_identity_count", 0))

        def _status_for(path_value: str) -> str:
            if not path_value:
                return "disabled"
            return state

        recognizer_state = _status_for(config.models.recognizer)
        if config.models.recognizer and state == "loaded" and not recognizer_enabled:
            recognizer_state = "idle_no_gallery"

        return {
            "detector": {
                "path": config.models.detector,
                "enabled": bool(config.models.detector),
                "status": _status_for(config.models.detector),
                "error": error,
            },
            "classifier": {
                "path": config.models.classifier,
                "enabled": bool(config.models.classifier),
                "status": _status_for(config.models.classifier),
                "error": error,
            },
            "recognizer": {
                "path": config.models.recognizer,
                "enabled": bool(config.models.recognizer),
                "status": recognizer_state,
                "error": error,
                "gallery_identity_count": gallery_identities,
            },
        }

    def _load_effective_config(self, config_path: str) -> RuntimeConfig:
        config = self.config_loader(config_path)
        if self._face_recognition_enabled_override is not None and not bool(self._face_recognition_enabled_override):
            config = disable_face_recognition(config)
        if self._alarm_enabled_override is not None:
            config = replace(
                config,
                alarm=replace(config.alarm, enabled=bool(self._alarm_enabled_override)),
            )
        config = self._apply_runtime_overrides(config)
        return config

    def _ensure_alarm_store(self):
        with self._lock:
            store = self._alarm_store
            runtime = self._runtime
            config_path = self.config_path
        if store is not None:
            return store
        if runtime is not None:
            store = getattr(runtime, "alarm_store", None)
            if store is not None:
                with self._lock:
                    self._alarm_store = store
                return store
        try:
            config = self._load_effective_config(config_path)
        except Exception:
            LOGGER.exception("failed to initialize preview alarm store: %s", config_path)
            return None
        store = build_alarm_store(config, event_bus=self.event_bus)
        with self._lock:
            if self._alarm_store is None:
                self._alarm_store = store
            return self._alarm_store

    def _apply_runtime_overrides(self, config: RuntimeConfig) -> RuntimeConfig:
        overrides = dict(self._runtime_overrides or {})
        if not overrides:
            return config

        input_path = str(overrides.get("input_path") or "").strip()
        output_path = str(overrides.get("output_path") or "").strip()
        stream_url = str(overrides.get("stream_url") or "").strip()

        if input_path:
            config = replace(
                config,
                input=replace(config.input, path=input_path),
            )
        if output_path:
            config = replace(
                config,
                output=replace(config.output, output_path=output_path),
            )
        if stream_url:
            config = replace(
                config,
                output=replace(config.output, stream_url=stream_url),
            )
        return config
