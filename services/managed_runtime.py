"""Managed runtime session with optional mediamtx lifecycle control."""

from __future__ import annotations

from pathlib import Path

from services.bootstrap import build_runtime
from services.config import RuntimeConfig
from stream.mediamtx import MediaMtxConfig, MediaMtxProcess, is_tcp_port_open


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ManagedRuntimeSession:
    """Wrap SourceRuntime with optional mediamtx startup and readiness checks."""

    def __init__(
        self,
        config: RuntimeConfig,
        event_bus=None,
        runtime_factory=None,
        mediamtx_process_factory=MediaMtxProcess,
        port_checker=is_tcp_port_open,
    ) -> None:
        self.config = config
        self.event_bus = event_bus
        self.runtime_factory = runtime_factory or (lambda cfg, bus: build_runtime(cfg, event_bus=bus))
        self.mediamtx_process_factory = mediamtx_process_factory
        self.port_checker = port_checker
        self._runtime = None
        self._mediamtx = None
        self._owns_mediamtx = False
        self._mediamtx_state = self._build_mediamtx_state(state="disabled")

    def start(self) -> None:
        self._ensure_stream_services()
        self._runtime = self.runtime_factory(self.config, self.event_bus)
        self._runtime.start()

    def stop(self) -> None:
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            runtime.stop()
        if self._owns_mediamtx and self._mediamtx is not None:
            self._mediamtx.stop()
            self._mediamtx_state = self._build_mediamtx_state(state="stopped", owned=True)
        self._mediamtx = None
        self._owns_mediamtx = False

    def wait(self, timeout: float | None = None) -> bool:
        if self._runtime is None:
            raise RuntimeError("managed runtime session is not started")
        return self._runtime.wait(timeout=timeout)

    def snapshot_metrics(self):
        if self._runtime is None:
            return {"sources": {}}
        return self._runtime.snapshot_metrics()

    def snapshot_input_info(self) -> dict[str, object]:
        if self._runtime is None or not hasattr(self._runtime, "snapshot_input_info"):
            return {}
        return dict(self._runtime.snapshot_input_info())

    def snapshot_output_info(self) -> dict[str, object]:
        if self._runtime is None or not hasattr(self._runtime, "snapshot_output_info"):
            return {}
        return dict(self._runtime.snapshot_output_info())

    def snapshot_services(self) -> dict[str, dict[str, object]]:
        return {"mediamtx": dict(self._mediamtx_state)}

    def snapshot_alarms(self, limit: int = 100, active_only: bool = False) -> list[dict[str, object]]:
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "snapshot_alarms"):
            return []
        return runtime.snapshot_alarms(limit=limit, active_only=active_only)

    def get_alarm(self, alarm_id: int) -> dict[str, object] | None:
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "get_alarm"):
            return None
        return runtime.get_alarm(alarm_id)

    def resolve_alarm_image(self, relative_path: str) -> Path | None:
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "resolve_alarm_image"):
            return None
        return runtime.resolve_alarm_image(relative_path)

    def set_alarm_enabled(self, enabled: bool) -> bool:
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "set_alarm_enabled"):
            return False
        return bool(runtime.set_alarm_enabled(enabled))

    def set_face_recognition_enabled(self, enabled: bool) -> bool:
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "set_face_recognition_enabled"):
            return False
        return bool(runtime.set_face_recognition_enabled(enabled))

    def set_face_identity_enabled(self, identity: str, enabled: bool) -> bool:
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "set_face_identity_enabled"):
            return False
        return bool(runtime.set_face_identity_enabled(identity, enabled))

    @property
    def result_enricher(self):
        if self._runtime is None:
            return None
        return getattr(self._runtime, "result_enricher", None)

    @property
    def alarm_store(self):
        if self._runtime is None:
            return None
        return getattr(self._runtime, "alarm_store", None)

    def _ensure_stream_services(self) -> None:
        service_config = self.config.mediamtx
        if not bool(service_config.enabled):
            self._mediamtx_state = self._build_mediamtx_state(state="disabled")
            return

        binary_path = self._resolve_path(service_config.binary_path)
        config_path = self._resolve_path(service_config.config_path) if service_config.config_path else ""
        process = self.mediamtx_process_factory(
            MediaMtxConfig(
                binary_path=binary_path,
                config_path=config_path,
                host=service_config.host,
                port=service_config.port,
                start_timeout_ms=service_config.start_timeout_ms,
                retry_interval_ms=service_config.retry_interval_ms,
            )
        )
        self._mediamtx = process

        if self.port_checker(service_config.host, service_config.port):
            self._mediamtx_state = self._build_mediamtx_state(
                state="ready_external",
                owned=False,
                binary_path=binary_path,
                config_path=config_path,
            )
            self._publish(
                "stream.mediamtx",
                {
                    "state": "ready_external",
                    "host": service_config.host,
                    "port": service_config.port,
                },
            )
            return

        if not bool(service_config.auto_start):
            self._mediamtx_state = self._build_mediamtx_state(
                state="unreachable",
                owned=False,
                binary_path=binary_path,
                config_path=config_path,
            )
            raise RuntimeError(
                f"mediamtx is not reachable on {service_config.host}:{service_config.port}; "
                "start it manually or enable mediamtx.auto_start"
            )

        process.start()
        self._owns_mediamtx = True
        if not process.wait_until_ready():
            self._mediamtx_state = self._build_mediamtx_state(
                state="start_timeout",
                owned=True,
                binary_path=binary_path,
                config_path=config_path,
            )
            raise RuntimeError(
                f"mediamtx did not become ready on {service_config.host}:{service_config.port} "
                f"within {service_config.start_timeout_ms} ms"
            )
        self._mediamtx_state = self._build_mediamtx_state(
            state="ready_started",
            owned=True,
            binary_path=binary_path,
            config_path=config_path,
        )
        self._publish(
            "stream.mediamtx",
            {
                "state": "ready_started",
                "host": service_config.host,
                "port": service_config.port,
            },
        )

    def _resolve_path(self, path_value: str) -> str:
        candidate = Path(str(path_value or "").strip()).expanduser()
        if candidate.is_absolute():
            return str(candidate)
        return str((PROJECT_ROOT / candidate).resolve())

    def _publish(self, topic: str, payload: dict[str, object]) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(topic=topic, source_id=self.config.source_id, payload=payload)

    def _build_mediamtx_state(
        self,
        state: str,
        owned: bool = False,
        binary_path: str = "",
        config_path: str = "",
    ) -> dict[str, object]:
        service_config = self.config.mediamtx
        return {
            "enabled": bool(service_config.enabled),
            "state": str(state),
            "host": str(service_config.host),
            "port": int(service_config.port),
            "auto_start": bool(service_config.auto_start),
            "owned": bool(owned),
            "binary_path": str(binary_path or service_config.binary_path or ""),
            "config_path": str(config_path or service_config.config_path or ""),
        }


def build_managed_runtime(config: RuntimeConfig, event_bus=None) -> ManagedRuntimeSession:
    return ManagedRuntimeSession(config=config, event_bus=event_bus)
