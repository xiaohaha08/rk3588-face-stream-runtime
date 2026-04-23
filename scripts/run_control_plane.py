"""Run the Flask control-plane server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.server import create_app
from services.config import load_runtime_config
from services.event_bus import EventBus
from services.runtime_manager import RuntimeManager
from utils.logging import setup_logging


def _resolve_config_path(raw_path: str) -> str:
    candidate = Path(str(raw_path or "")).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    project_relative = (PROJECT_ROOT / candidate).resolve()
    if project_relative.exists():
        return str(project_relative)
    return str(candidate.resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the video control plane")
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "configs" / "usb_camera_rtsp.json"),
        help="runtime config path",
    )
    parser.add_argument("--host", help="override api host")
    parser.add_argument("--port", type=int, help="override api port")
    parser.add_argument("--no-autostart", action="store_true", help="do not auto start runtime")
    args = parser.parse_args()

    config_path = _resolve_config_path(args.config)
    config = load_runtime_config(config_path)
    setup_logging(config.logging.level)
    manager = RuntimeManager(
        config_path=config_path,
        event_bus=EventBus(max_events=config.api.event_buffer_size),
    )

    if config.api.auto_start and not args.no_autostart:
        try:
            manager.start()
        except Exception as exc:
            print(
                json.dumps(
                    {"warning": "runtime_autostart_failed", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )

    try:
        app = create_app(manager)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "hint": "Install flask before running the control plane. Install simple-websocket for /ws support.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        manager.stop()
        return 2

    host = args.host or config.api.host
    port = args.port or config.api.port
    try:
        app.run(host=host, port=port, debug=config.api.debug, threaded=True)
    finally:
        manager.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
