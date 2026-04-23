"""Flask API, SSE, WebSocket, and dashboard entrypoints."""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from queue import Empty
import subprocess
from urllib.parse import quote

from api.serializers import (
    build_stream_urls,
    infer_public_host,
    serialize_runtime_config,
    serialize_source_entry,
)
from services.config import GalleryConfig
from services.face_library import FaceLibraryService, decode_image_base64
from services.runtime_manager import RuntimeManager
from services.system_monitor import SystemMonitor


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"
CONFIG_ROOT = PROJECT_ROOT / "configs"
LOCAL_VIDEO_ROOT = PROJECT_ROOT / "video"
LOCAL_VIDEO_FOLDERS = ("input", "output")
LOCAL_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".ts", ".m4v", ".webm"}


def _config_presets() -> list[dict[str, str]]:
    presets = [
        ("USB Camera", "USB Camera RTSP", "USB camera input, inference, and RTSP publish.", "usb_camera_rtsp.json"),
        ("RTSP Input", "RTSP Input RTSP", "Pull an RTSP stream, run inference, and republish it.", "rtsp_input_rtsp.json"),
        ("Video File", "Video File RTSP", "Run inference on a local video and publish it over RTSP.", "video_file_rtsp.json"),
        ("Video File", "Video File Save", "Run inference on a local video and save the processed result.", "video_file_save.json"),
    ]
    return [
        {
            "category": category,
            "label": label,
            "description": description,
            "path": str((CONFIG_ROOT / filename).resolve()),
            "filename": filename,
        }
        for category, label, description, filename in presets
    ]


def create_app(manager: RuntimeManager):
    """Create the control-plane Flask application."""

    try:
        from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Flask is not installed. Install flask before running the control plane. "
            "Install simple-websocket as well if you want WebSocket support."
        ) from exc

    app = Flask(__name__)
    system_monitor = SystemMonitor()
    face_library_service = FaceLibraryService(project_root=PROJECT_ROOT)

    def _local_video_root() -> Path:
        LOCAL_VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
        return LOCAL_VIDEO_ROOT.resolve()

    def _local_video_dir(folder: str) -> Path:
        normalized = str(folder or "").strip().lower()
        if normalized not in LOCAL_VIDEO_FOLDERS:
            raise ValueError(f"unsupported local video folder: {folder}")
        directory = (_local_video_root() / normalized).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _list_local_videos(folder: str) -> list[dict[str, object]]:
        directory = _local_video_dir(folder)
        items: list[dict[str, object]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in LOCAL_VIDEO_EXTENSIONS:
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "folder": folder,
                    "size_bytes": int(stat.st_size),
                    "modified_at_ms": int(stat.st_mtime * 1000),
                    "url": f"/api/local-videos/{folder}/{quote(path.name, safe='')}",
                    "preview_url": f"/api/local-videos-preview/{folder}/{quote(path.name, safe='')}",
                    "mime_type": mimetypes.guess_type(path.name)[0] or "video/mp4",
                }
            )
        return items

    def _local_video_bundle(config=None) -> dict[str, object]:
        input_dir = _local_video_dir("input")
        output_dir = _local_video_dir("output")
        selected_input = ""
        selected_output = ""
        if config is not None:
            if str(config.input.kind or "").strip().lower() == "video_file":
                selected_input = str(config.input.path or "")
            if str(config.output.sink or "").strip().lower() == "ffmpeg_file":
                selected_output = str(config.output.output_path or "")
        return {
            "root": str(_local_video_root()),
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "input_files": _list_local_videos("input"),
            "output_files": _list_local_videos("output"),
            "selected_input": selected_input,
            "selected_output": selected_output,
        }

    def _safe_local_video_file(folder: str, filename: str) -> Path:
        base_dir = _local_video_dir(folder)
        candidate = (base_dir / filename).resolve()
        if not candidate.is_file() or candidate.parent != base_dir:
            raise FileNotFoundError(filename)
        if candidate.suffix.lower() not in LOCAL_VIDEO_EXTENSIONS:
            raise FileNotFoundError(filename)
        return candidate

    def _sanitize_local_video_filename(filename: str) -> str:
        raw_name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not raw_name:
            raise ValueError("video file name is required")
        sanitized = "".join(
            char if char.isalnum() or char in "._- ()[]{}" else "_"
            for char in raw_name
        ).strip(" .")
        path_name = Path(sanitized)
        suffix = path_name.suffix.lower()
        if suffix not in LOCAL_VIDEO_EXTENSIONS:
            supported = ", ".join(sorted(LOCAL_VIDEO_EXTENSIONS))
            raise ValueError(f"unsupported video extension; supported: {supported}")
        stem = path_name.stem.strip(" ._")[:120] or "video"
        return f"{stem}{suffix}"

    def _unique_local_video_path(folder: str, filename: str) -> Path:
        directory = _local_video_dir(folder)
        candidate = (directory / filename).resolve()
        if candidate.parent != directory:
            raise ValueError("video file must be saved inside the local video folder")
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for index in range(1, 1000):
            numbered = (directory / f"{stem}_{index}{suffix}").resolve()
            if numbered.parent == directory and not numbered.exists():
                return numbered
        raise ValueError("too many files with the same video name")

    def _preview_cache_dir(folder: str) -> Path:
        directory = (_local_video_root() / ".preview_cache" / folder).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _preview_cache_path(source_path: Path, folder: str) -> Path:
        return (_preview_cache_dir(folder) / f"{source_path.stem}__preview.mp4").resolve()

    def _ensure_browser_preview(folder: str, filename: str) -> Path:
        source_path = _safe_local_video_file(folder, filename)
        preview_path = _preview_cache_path(source_path, folder)
        if preview_path.is_file() and preview_path.stat().st_mtime >= source_path.stat().st_mtime:
            return preview_path

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(preview_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg not found; cannot build browser preview") from exc
        if completed.returncode != 0 or not preview_path.is_file():
            preview_path.unlink(missing_ok=True)
            error_tail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise RuntimeError(f"ffmpeg preview build failed: {error_tail or 'unknown error'}")
        return preview_path

    def _is_local_video_config(config) -> bool:
        return config is not None and str(config.input.kind or "").strip().lower() == "video_file"

    def _default_output_path(input_path: str) -> str:
        source = Path(str(input_path or "").strip())
        if not source.name:
            source = Path("video.mp4")
        output_dir = _local_video_dir("output")
        suffix = source.suffix or ".mp4"
        stem = source.stem or "video"
        return str((output_dir / f"{stem}_processed{suffix}").resolve())

    def _resolve_runtime_overrides(config, body: dict[str, object]) -> dict[str, object]:
        if not _is_local_video_config(config):
            return {}

        overrides: dict[str, object] = {}
        selected_input = str(body.get("local_video_input") or "").strip()
        if selected_input:
            candidate = Path(selected_input).expanduser()
            if not candidate.is_absolute():
                candidate = (_local_video_dir("input") / selected_input).resolve()
            input_dir = _local_video_dir("input")
            if not candidate.is_file() or input_dir not in candidate.parents:
                raise ValueError(f"local input video not found: {selected_input}")
            overrides["input_path"] = str(candidate)
        elif str(config.input.path or "").strip():
            overrides["input_path"] = str(config.input.path)

        output_sink = str(config.output.sink or "").strip().lower()
        if output_sink == "ffmpeg_file":
            output_path = str(body.get("local_video_output") or "").strip()
            if output_path:
                output_candidate = Path(output_path).expanduser()
                if not output_candidate.is_absolute():
                    output_candidate = (_local_video_dir("output") / output_path).resolve()
                output_dir = _local_video_dir("output")
                if output_dir != output_candidate.parent.resolve():
                    raise ValueError("local output video must be saved inside the output folder")
                overrides["output_path"] = str(output_candidate)
            else:
                input_path = str(overrides.get("input_path") or config.input.path or "").strip()
                overrides["output_path"] = _default_output_path(input_path)
        return overrides

    def _public_host() -> str:
        return infer_public_host(request.host, fallback="127.0.0.1")

    def _gallery_config() -> GalleryConfig:
        config = manager.preview_config()
        if config is None:
            return GalleryConfig()
        return config.gallery

    def _resolve_model_path(path_value: str) -> str:
        value = str(path_value or "").strip()
        if not value:
            return ""
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return str(candidate)
        return str((PROJECT_ROOT / candidate).resolve())

    def _parse_enabled_flag(raw_value, *, default: bool | None = None) -> bool | None:
        if raw_value is None:
            return default
        if isinstance(raw_value, str):
            return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw_value)

    def _snapshot_bundle() -> dict[str, object]:
        state = manager.snapshot_state()
        config = manager.preview_config()
        public_host = _public_host()
        return {
            "runtime": state,
            "config": serialize_runtime_config(config),
            "sources": [serialize_source_entry(config, state, public_host=public_host)],
            "streams": build_stream_urls(
                config.output.stream_url if config is not None else "",
                public_host=public_host,
                mediamtx=config.mediamtx if config is not None else None,
            ),
            "system": system_monitor.snapshot(),
            "recent_events": manager.recent_events(limit=20),
            "alarms": manager.recent_alarms(limit=100),
            "websocket_enabled": bool(config.api.enable_websocket) if config is not None else False,
            "config_presets": _config_presets(),
            "local_videos": _local_video_bundle(config),
        }

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        return send_from_directory(WEB_ROOT, "dashboard.html")

    @app.get("/assets/<path:filename>")
    def assets(filename: str):
        return send_from_directory(WEB_ROOT, filename)

    @app.get("/api/health")
    def health():
        state = manager.snapshot_state()
        config = manager.preview_config()
        return jsonify(
            {
                "status": "ok",
                "runtime": state["state"],
                "source_id": state.get("source_id", ""),
                "websocket_enabled": bool(config.api.enable_websocket) if config is not None else False,
                "sse_enabled": True,
            }
        )

    @app.get("/api/runtime")
    def runtime_state():
        return jsonify(manager.snapshot_state())

    @app.post("/api/runtime/start")
    def runtime_start():
        body = request.get_json(silent=True) or {}
        config_path = str(body.get("config_path") or "").strip() or None
        try:
            preview_config = manager.config_loader(config_path or manager.config_path)
            runtime_overrides = _resolve_runtime_overrides(preview_config, body)
            state = manager.start(config_path=config_path, runtime_overrides=runtime_overrides)
            return jsonify(state)
        except ValueError as exc:
            return jsonify({"error": str(exc), "state": "error"}), 400
        except Exception as exc:
            LOGGER.exception("runtime start failed")
            return jsonify({"error": str(exc), "state": "error"}), 500

    @app.post("/api/runtime/stop")
    def runtime_stop():
        return jsonify(manager.stop())

    @app.post("/api/runtime/alarm")
    def runtime_alarm():
        body = request.get_json(silent=True) or {}
        enabled = _parse_enabled_flag(body.get("enabled"), default=None)
        if enabled is None:
            return jsonify({"error": "enabled is required"}), 400
        try:
            state = manager.set_alarm_enabled(enabled)
            return jsonify(state)
        except Exception as exc:
            LOGGER.exception("alarm toggle failed")
            return jsonify({"error": str(exc), "state": "error"}), 400

    @app.post("/api/runtime/face-recognition")
    def runtime_face_recognition():
        body = request.get_json(silent=True) or {}
        enabled = _parse_enabled_flag(body.get("enabled"), default=None)
        if enabled is None:
            return jsonify({"error": "enabled is required"}), 400
        try:
            state = manager.set_face_recognition_enabled(enabled)
            return jsonify(state)
        except Exception as exc:
            LOGGER.exception("face recognition toggle failed")
            return jsonify({"error": str(exc), "state": "error"}), 400

    @app.get("/api/config")
    def config_snapshot():
        config = manager.preview_config()
        return jsonify(
            {
                "config_path": manager.config_path,
                "config": serialize_runtime_config(config),
            }
        )

    @app.get("/api/config-presets")
    def config_presets():
        return jsonify({"presets": _config_presets()})

    @app.get("/api/local-videos")
    def local_videos():
        config = manager.preview_config()
        return jsonify(_local_video_bundle(config))

    @app.post("/api/local-videos/<folder>")
    def local_video_upload(folder: str):
        storage = request.files.get("file") or request.files.get("video")
        if storage is None or not storage.filename:
            return jsonify({"error": "video file is required"}), 400
        try:
            filename = _sanitize_local_video_filename(storage.filename)
            target_path = _unique_local_video_path(folder, filename)
            storage.save(str(target_path))
            if not target_path.is_file() or target_path.stat().st_size <= 0:
                target_path.unlink(missing_ok=True)
                return jsonify({"error": "uploaded video is empty"}), 400
            config = manager.preview_config()
            item = next(
                (item for item in _list_local_videos(folder) if item["path"] == str(target_path)),
                None,
            )
            return jsonify({"item": item, "local_videos": _local_video_bundle(config)}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            LOGGER.exception("local video upload failed: %s", folder)
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/local-videos/<folder>/<path:filename>")
    def local_video_file(folder: str, filename: str):
        try:
            video_path = _safe_local_video_file(folder, filename)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except FileNotFoundError:
            return jsonify({"error": "video not found"}), 404
        return send_file(str(video_path), conditional=True)

    @app.delete("/api/local-videos/<folder>/<path:filename>")
    def local_video_delete(folder: str, filename: str):
        try:
            video_path = _safe_local_video_file(folder, filename)
            preview_path = _preview_cache_path(video_path, folder)
            video_path.unlink()
            preview_path.unlink(missing_ok=True)
            config = manager.preview_config()
            return jsonify({"deleted": str(video_path), "local_videos": _local_video_bundle(config)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except FileNotFoundError:
            return jsonify({"error": "video not found"}), 404
        except Exception as exc:
            LOGGER.exception("local video delete failed: %s/%s", folder, filename)
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/local-videos-preview/<folder>/<path:filename>")
    def local_video_preview_file(folder: str, filename: str):
        try:
            preview_path = _ensure_browser_preview(folder, filename)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except FileNotFoundError:
            return jsonify({"error": "video not found"}), 404
        except RuntimeError as exc:
            LOGGER.exception("local video preview build failed: %s/%s", folder, filename)
            return jsonify({"error": str(exc)}), 500
        return send_file(str(preview_path), mimetype="video/mp4", conditional=True)

    @app.get("/api/face-library/identities")
    def face_library_list_identities():
        keyword = request.args.get("q", default="", type=str)
        try:
            payload = face_library_service.list_identities(_gallery_config(), keyword=keyword)
            for item in payload.get("items", []):
                sample_id = int(item.get("latest_sample_id") or 0)
                item["image_url"] = f"/api/face-library/samples/{sample_id}/image" if sample_id > 0 else ""
            return jsonify(payload)
        except Exception as exc:
            LOGGER.exception("face library list identities failed")
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/face-library/identities")
    def face_library_create_identity():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        note = str(body.get("note") or "").strip()
        enabled = bool(_parse_enabled_flag(body.get("enabled"), default=True))
        try:
            item = face_library_service.create_identity(_gallery_config(), name=name, note=note, enabled=enabled)
            return jsonify({"item": item}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            LOGGER.exception("face library create identity failed")
            return jsonify({"error": str(exc)}), 500

    @app.put("/api/face-library/identities/<int:identity_id>")
    def face_library_update_identity(identity_id: int):
        body = request.get_json(silent=True) or {}
        has_name = "name" in body
        has_note = "note" in body
        has_enabled = "enabled" in body
        if not has_name and not has_note and not has_enabled:
            return jsonify({"error": "name or note or enabled is required"}), 400
        name = str(body.get("name") or "").strip() if has_name else None
        note = str(body.get("note") or "").strip() if has_note else None
        enabled = _parse_enabled_flag(body.get("enabled"), default=None) if has_enabled else None
        try:
            item = face_library_service.update_identity(
                _gallery_config(),
                identity_id=identity_id,
                name=name,
                note=note,
                enabled=enabled,
            )
            if enabled is not None:
                manager.set_face_identity_enabled(str(item.get("name") or ""), bool(item.get("enabled")))
            return jsonify({"item": item})
        except KeyError as exc:
            return jsonify({"error": str(exc.args[0] if exc.args else exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            LOGGER.exception("face library update identity failed")
            return jsonify({"error": str(exc)}), 500

    @app.delete("/api/face-library/identities/<int:identity_id>")
    def face_library_delete_identity(identity_id: int):
        try:
            result = face_library_service.delete_identity(_gallery_config(), identity_id=identity_id)
            return jsonify({"deleted": result})
        except KeyError as exc:
            return jsonify({"error": str(exc.args[0] if exc.args else exc)}), 404
        except Exception as exc:
            LOGGER.exception("face library delete identity failed")
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/face-library/identities/<int:identity_id>/samples")
    def face_library_list_samples(identity_id: int):
        try:
            payload = face_library_service.list_samples(_gallery_config(), identity_id=identity_id)
            for item in payload.get("items", []):
                item["image_url"] = f"/api/face-library/samples/{int(item['id'])}/image"
            return jsonify(payload)
        except KeyError as exc:
            return jsonify({"error": str(exc.args[0] if exc.args else exc)}), 404
        except Exception as exc:
            LOGGER.exception("face library list samples failed")
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/face-library/identities/<int:identity_id>/samples")
    def face_library_add_sample(identity_id: int):
        body = request.get_json(silent=True) or {}
        image_payload = body.get("image_base64")
        try:
            config = manager.config_loader(manager.config_path)
        except Exception as exc:
            LOGGER.exception("face library load base config failed")
            return jsonify({"error": str(exc)}), 400

        detector_model = _resolve_model_path(config.models.detector)
        recognizer_model = _resolve_model_path(config.models.recognizer)
        if not detector_model:
            return jsonify({"error": "models.detector is required"}), 400
        if not recognizer_model:
            return jsonify({"error": "models.recognizer is required"}), 400
        try:
            image_bytes = decode_image_base64(str(image_payload or ""))
            sample = face_library_service.add_sample_from_image_bytes(
                gallery_config=config.gallery,
                identity_id=identity_id,
                image_bytes=image_bytes,
                detector_model_path=detector_model,
                recognizer_model_path=recognizer_model,
                detector_config=config.detector,
                recognizer_config=config.recognizer,
                core_id=int(config.tracking.recognizer_core_id),
            )
            sample["image_url"] = f"/api/face-library/samples/{int(sample['id'])}/image"
            return jsonify({"item": sample}), 201
        except KeyError as exc:
            return jsonify({"error": str(exc.args[0] if exc.args else exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            LOGGER.exception("face library add sample failed")
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/face-library/samples/<int:sample_id>/image")
    def face_library_sample_image(sample_id: int):
        image_path = face_library_service.get_sample_image_path(_gallery_config(), sample_id=sample_id)
        if image_path is None:
            return jsonify({"error": f"sample image not found: {sample_id}"}), 404
        return send_file(str(image_path))

    @app.delete("/api/face-library/samples/<int:sample_id>")
    def face_library_delete_sample(sample_id: int):
        try:
            result = face_library_service.delete_sample(_gallery_config(), sample_id=sample_id)
            return jsonify({"deleted": result})
        except KeyError as exc:
            return jsonify({"error": str(exc.args[0] if exc.args else exc)}), 404
        except Exception as exc:
            LOGGER.exception("face library delete sample failed")
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/face-library/runtime/reload")
    def face_library_reload_runtime():
        state = manager.snapshot_state()
        if str(state.get("state") or "") not in {"running", "starting"}:
            return jsonify({"reloaded": False, "runtime": state, "reason": "runtime_not_running"})
        try:
            manager.stop()
            restarted = manager.start(config_path=manager.config_path)
            return jsonify({"reloaded": True, "runtime": restarted})
        except Exception as exc:
            LOGGER.exception("face library runtime reload failed")
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/metrics")
    def metrics():
        return jsonify(manager.snapshot_metrics())

    @app.get("/api/system")
    def system_state():
        return jsonify({"system": system_monitor.snapshot()})

    @app.get("/api/models")
    def models():
        return jsonify({"models": manager.snapshot_state()["models"]})

    @app.get("/api/sources")
    def sources():
        state = manager.snapshot_state()
        config = manager.preview_config()
        public_host = _public_host()
        return jsonify({"sources": [serialize_source_entry(config, state, public_host=public_host)]})

    @app.get("/api/streams")
    def streams():
        config = manager.preview_config()
        return jsonify(
            {
                "streams": build_stream_urls(
                    config.output.stream_url if config is not None else "",
                    public_host=_public_host(),
                    mediamtx=config.mediamtx if config is not None else None,
                )
            }
        )

    @app.get("/api/frontend/bootstrap")
    def frontend_bootstrap():
        return jsonify(_snapshot_bundle())

    @app.get("/api/events")
    def events():
        limit = request.args.get("limit", default=100, type=int)
        topic = request.args.get("topic", default=None, type=str)
        return jsonify({"events": manager.recent_events(limit=limit, topic=topic)})

    @app.get("/api/alarms")
    def alarms():
        limit = request.args.get("limit", default=100, type=int)
        active_only = bool(request.args.get("active_only", default=0, type=int))
        return jsonify({"alarms": manager.recent_alarms(limit=limit, active_only=active_only)})

    @app.delete("/api/alarms")
    def alarms_delete_all():
        try:
            result = manager.delete_all_alarms()
            return jsonify({"deleted": result})
        except Exception as exc:
            LOGGER.exception("delete all alarms failed")
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/alarms/<int:alarm_id>")
    def alarm_detail(alarm_id: int):
        alarm = manager.get_alarm(alarm_id)
        if alarm is None:
            return jsonify({"error": "alarm not found"}), 404
        return jsonify({"alarm": alarm})

    @app.delete("/api/alarms/<int:alarm_id>")
    def alarm_delete(alarm_id: int):
        try:
            result = manager.delete_alarm(alarm_id)
            return jsonify({"deleted": result})
        except KeyError as exc:
            return jsonify({"error": str(exc.args[0] if exc.args else exc)}), 404
        except Exception as exc:
            LOGGER.exception("delete alarm failed")
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/alarms/images/<path:relative_path>")
    def alarm_image(relative_path: str):
        image_path = manager.resolve_alarm_image(relative_path)
        if image_path is None or not image_path.is_file():
            return jsonify({"error": "alarm image not found"}), 404
        return send_file(str(image_path))

    @app.get("/api/stream")
    def stream_events():
        limit = request.args.get("limit", default=20, type=int)
        topic = request.args.get("topic", default=None, type=str)

        @stream_with_context
        def _generate():
            for event in manager.recent_events(limit=limit, topic=topic):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            subscriber_id, queue_obj = manager.event_bus.subscribe(max_queue=64)
            try:
                while True:
                    try:
                        event = queue_obj.get(timeout=15.0)
                    except Empty:
                        yield "event: heartbeat\ndata: {}\n\n"
                        continue
                    if topic and event.topic != topic:
                        continue
                    yield f"data: {event.as_json()}\n\n"
            finally:
                manager.event_bus.unsubscribe(subscriber_id)

        return Response(_generate(), mimetype="text/event-stream")

    @app.route("/ws")
    def websocket_stream():
        config = manager.preview_config()
        if config is not None and not bool(config.api.enable_websocket):
            return jsonify({"error": "websocket is disabled by api.enable_websocket"}), 404
        try:
            from simple_websocket import ConnectionClosed, Server
        except ModuleNotFoundError:
            return jsonify({"error": "simple-websocket is not installed; use /api/stream SSE instead."}), 501

        ws = Server.accept(request.environ)
        ws.send(json.dumps({"type": "snapshot", "payload": _snapshot_bundle()}, ensure_ascii=False))
        subscriber_id, queue_obj = manager.event_bus.subscribe(max_queue=64)
        try:
            while True:
                try:
                    event = queue_obj.get(timeout=15.0)
                    ws.send(event.as_json())
                except Empty:
                    ws.send(json.dumps({"type": "heartbeat"}, ensure_ascii=False))
        except ConnectionClosed:
            return ""
        finally:
            manager.event_bus.unsubscribe(subscriber_id)

    return app
