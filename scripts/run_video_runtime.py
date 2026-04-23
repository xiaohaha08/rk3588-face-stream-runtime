"""Run the configured video runtime pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.config import load_runtime_config
from services.managed_runtime import build_managed_runtime
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
    parser = argparse.ArgumentParser(description="Run the production video inference runtime")
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "configs" / "usb_camera_rtsp.json"),
        help="runtime config path",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="overall wait timeout in seconds; use 0 or a negative value to run until interrupted",
    )
    args = parser.parse_args()

    config = None
    runtime = None
    try:
        config_path = _resolve_config_path(args.config)
        config = load_runtime_config(config_path)
        setup_logging(config.logging.level)
        runtime = build_managed_runtime(config)
        runtime.start()
        wait_timeout = None if args.timeout is None or float(args.timeout) <= 0 else float(args.timeout)
        ok = runtime.wait(timeout=wait_timeout)
        snapshot = runtime.snapshot_metrics()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    except KeyboardInterrupt:
        if runtime is not None:
            snapshot = runtime.snapshot_metrics()
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {
                    "source_id": config.source_id if config is not None else "",
                    "worker_mode": config.pipeline.worker_mode if config is not None else "",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
