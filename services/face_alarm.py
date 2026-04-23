"""Face-recognition alarm aggregation, persistence, and snapshot storage."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import numpy as np

from pipelines.types import CapturedFrame, InferenceResult


LOGGER = logging.getLogger(__name__)


def _format_local_time(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return ""
    return datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _default_frame_saver(frame_rgb: np.ndarray, output_path: Path) -> bool:
    try:
        import cv2  # type: ignore
    except ModuleNotFoundError:
        return False

    if frame_rgb is None or frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rgb = np.ascontiguousarray(frame_rgb)
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    ok, encoded = cv2.imencode(".jpg", bgr)
    if not ok:
        return False
    encoded.tofile(str(output_path))
    return True


@dataclass
class AlarmSnapshot:
    timestamp_ms: int
    image_rel_path: str

    def as_dict(self) -> dict[str, object]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "captured_at": _format_local_time(self.timestamp_ms),
            "image_path": self.image_rel_path,
            "image_url": f"/api/alarms/images/{quote(self.image_rel_path)}",
        }


@dataclass
class AlarmRecord:
    alarm_id: int
    source_id: str
    identity: str
    appeared_at_ms: int
    last_seen_ms: int
    disappeared_at_ms: int | None = None
    snapshots: list[AlarmSnapshot] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.disappeared_at_ms is None

    def as_dict(self, now_ms: int | None = None) -> dict[str, object]:
        active = self.active
        end_ms = now_ms if active and now_ms is not None else self.disappeared_at_ms
        if end_ms is None:
            end_ms = self.last_seen_ms
        duration_ms = max(0, int(end_ms) - int(self.appeared_at_ms))
        return {
            "alarm_id": self.alarm_id,
            "source_id": self.source_id,
            "identity": self.identity,
            "active": active,
            "appeared_at_ms": self.appeared_at_ms,
            "appeared_at": _format_local_time(self.appeared_at_ms),
            "last_seen_ms": self.last_seen_ms,
            "last_seen_at": _format_local_time(self.last_seen_ms),
            "left_at_ms": self.disappeared_at_ms,
            "left_at": _format_local_time(self.disappeared_at_ms),
            "duration_ms": duration_ms,
            "snapshot_count": len(self.snapshots),
            "snapshots": [item.as_dict() for item in self.snapshots],
        }


@dataclass
class _ActiveAlarm:
    record: AlarmRecord
    last_snapshot_ms: int


class FaceAlarmStore:
    """Track appearance windows for recognized identities and persist alarm history."""

    def __init__(
        self,
        source_id: str,
        image_root: str,
        unknown_label: str = "unknown",
        snapshot_interval_seconds: float = 5.0,
        leave_timeout_seconds: float = 2.0,
        max_events: int = 200,
        enabled: bool = False,
        metadata_path: str | None = None,
        event_publisher=None,
        frame_saver: Callable[[np.ndarray, Path], bool] | None = None,
    ) -> None:
        self.source_id = str(source_id)
        self.unknown_label = str(unknown_label or "unknown").strip().lower()
        self.image_root = Path(str(image_root or "outputs/face_alarm_images")).expanduser().resolve()
        self.image_root.mkdir(parents=True, exist_ok=True)
        self.metadata_path = Path(str(metadata_path or (self.image_root / "alarm_records.db"))).expanduser().resolve()
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_interval_ms = max(500, int(float(snapshot_interval_seconds) * 1000.0))
        self.leave_timeout_ms = max(300, int(float(leave_timeout_seconds) * 1000.0))
        self.max_events = max(1, int(max_events))
        self.event_publisher = event_publisher
        self._frame_saver = frame_saver or _default_frame_saver

        self._lock = threading.RLock()
        self._active: dict[str, _ActiveAlarm] = {}
        self._enabled = bool(enabled)
        self._closed = False

        self._initialize_storage()
        self._next_alarm_id = self._load_next_alarm_id()

    def set_enabled(self, enabled: bool) -> bool:
        publish_items: list[tuple[str, dict[str, object]]] = []
        now_ms = int(time.time() * 1000.0)
        with self._lock:
            next_enabled = bool(enabled)
            if self._enabled == next_enabled:
                return self._enabled
            self._enabled = next_enabled
            if not self._enabled:
                publish_items.extend(self._close_active_locked(now_ms=now_ms))
        for topic, payload in publish_items:
            self._publish(topic, payload)
        return self._enabled

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_identity_enabled(self, identity: str, enabled: bool) -> bool:
        publish_items: list[tuple[str, dict[str, object]]] = []
        name = str(identity or "").strip()
        if not name:
            return False
        if bool(enabled):
            return True
        now_ms = int(time.time() * 1000.0)
        with self._lock:
            active = self._active.pop(name, None)
            if active is None:
                return False
            active.record.disappeared_at_ms = now_ms
            self._update_alarm_end(active.record.alarm_id, disappeared_at_ms=now_ms)
            publish_items.append(("alarm.face.end", active.record.as_dict(now_ms=now_ms)))
        for topic, payload in publish_items:
            self._publish(topic, payload)
        return True

    def process_frame(
        self,
        frame: CapturedFrame,
        result: InferenceResult | None,
        snapshot_frame: np.ndarray | None = None,
    ) -> None:
        now_ms = int(float(frame.capture_wall_time) * 1000.0)
        snapshot_source = snapshot_frame if snapshot_frame is not None else frame.frame
        snapshot_requests: list[tuple[int, str, int, np.ndarray]] = []
        publish_items: list[tuple[str, dict[str, object]]] = []

        with self._lock:
            if self._closed:
                return
            if not self._enabled:
                return

            seen_identities = self._extract_target_identities(result)
            for identity in sorted(seen_identities):
                active = self._active.get(identity)
                if active is None:
                    record = AlarmRecord(
                        alarm_id=self._next_alarm_id,
                        source_id=self.source_id,
                        identity=identity,
                        appeared_at_ms=now_ms,
                        last_seen_ms=now_ms,
                    )
                    self._next_alarm_id += 1
                    self._insert_alarm(record)
                    active = _ActiveAlarm(record=record, last_snapshot_ms=-10_000_000)
                    self._active[identity] = active
                    publish_items.append(("alarm.face.start", record.as_dict(now_ms=now_ms)))

                active.record.last_seen_ms = now_ms
                self._update_alarm_last_seen(active.record.alarm_id, last_seen_ms=now_ms)
                if now_ms - active.last_snapshot_ms >= self.snapshot_interval_ms:
                    active.last_snapshot_ms = now_ms
                    snapshot_requests.append((active.record.alarm_id, active.record.identity, now_ms, snapshot_source))

            for identity in list(self._active.keys()):
                if identity in seen_identities:
                    continue
                active = self._active[identity]
                if now_ms - active.record.last_seen_ms < self.leave_timeout_ms:
                    continue
                active.record.disappeared_at_ms = now_ms
                self._update_alarm_end(active.record.alarm_id, disappeared_at_ms=now_ms)
                self._active.pop(identity, None)
                publish_items.append(("alarm.face.end", active.record.as_dict(now_ms=now_ms)))

        for alarm_id, identity, timestamp_ms, frame_rgb in snapshot_requests:
            snapshot = self._capture_snapshot(
                alarm_id=alarm_id,
                identity=identity,
                timestamp_ms=timestamp_ms,
                frame_rgb=frame_rgb,
            )
            if snapshot is not None:
                publish_items.append(("alarm.face.snapshot", snapshot))

        for topic, payload in publish_items:
            self._publish(topic, payload)

    def recent(self, limit: int = 100, active_only: bool = False) -> list[dict[str, object]]:
        max_items = max(0, int(limit))
        if max_items == 0:
            return []
        with self._lock:
            with self._connect() as conn:
                return self._fetch_alarm_dicts(conn, limit=max_items, active_only=active_only)

    def get(self, alarm_id: int) -> dict[str, object] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT alarm_id, source_id, identity, appeared_at_ms, last_seen_ms, disappeared_at_ms
                    FROM alarms
                    WHERE alarm_id = ?
                    """,
                    (int(alarm_id),),
                ).fetchone()
                if row is None:
                    return None
                return self._row_to_record(conn, row).as_dict(now_ms=int(time.time() * 1000.0))

    def delete(self, alarm_id: int) -> dict[str, object]:
        removed = int(alarm_id)
        deleted_images = 0
        with self._lock:
            active_identity = None
            for identity, active in list(self._active.items()):
                if int(active.record.alarm_id) == removed:
                    active_identity = identity
                    break
            if active_identity is not None:
                self._active.pop(active_identity, None)

            with self._connect() as conn:
                snapshot_rows = conn.execute(
                    "SELECT image_rel_path FROM alarm_snapshots WHERE alarm_id = ?",
                    (removed,),
                ).fetchall()
                deleted = conn.execute("DELETE FROM alarms WHERE alarm_id = ?", (removed,))
                conn.commit()
            if int(deleted.rowcount or 0) <= 0:
                raise KeyError(f"alarm not found: {removed}")

        for row in snapshot_rows:
            image_path = self.resolve_image_path(str(row["image_rel_path"] or ""))
            if image_path is None:
                continue
            try:
                if image_path.is_file():
                    image_path.unlink()
                    deleted_images += 1
            except OSError:
                LOGGER.warning("failed to remove alarm image: %s", image_path, exc_info=True)

        return {"alarm_id": removed, "deleted_images": deleted_images}

    def delete_all(self) -> dict[str, object]:
        deleted_images = 0
        deleted_alarms = 0
        with self._lock:
            self._active.clear()
            with self._connect() as conn:
                snapshot_rows = conn.execute("SELECT image_rel_path FROM alarm_snapshots").fetchall()
                count_row = conn.execute("SELECT COUNT(*) AS total FROM alarms").fetchone()
                deleted_alarms = int(count_row["total"] or 0) if count_row is not None else 0
                conn.execute("DELETE FROM alarm_snapshots")
                conn.execute("DELETE FROM alarms")
                conn.commit()
            self._next_alarm_id = 1

        for row in snapshot_rows:
            image_path = self.resolve_image_path(str(row["image_rel_path"] or ""))
            if image_path is None:
                continue
            try:
                if image_path.is_file():
                    image_path.unlink()
                    deleted_images += 1
            except OSError:
                LOGGER.warning("failed to remove alarm image: %s", image_path, exc_info=True)

        return {"deleted_alarms": deleted_alarms, "deleted_images": deleted_images}

    def resolve_image_path(self, relative_path: str) -> Path | None:
        value = str(relative_path or "").strip()
        if not value:
            return None
        candidate = (self.image_root / value).resolve()
        try:
            candidate.relative_to(self.image_root)
        except ValueError:
            return None
        return candidate

    def close(self) -> None:
        publish_items: list[tuple[str, dict[str, object]]] = []
        now_ms = int(time.time() * 1000.0)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            publish_items.extend(self._close_active_locked(now_ms=now_ms))
        for topic, payload in publish_items:
            self._publish(topic, payload)

    def _extract_target_identities(self, result: InferenceResult | None) -> set[str]:
        if result is None:
            return set()
        identities: set[str] = set()
        for detection in result.detections:
            if str(detection.label or "").strip().lower() != "face":
                continue
            attrs = dict(detection.attributes or {})
            identity = str(attrs.get("identity", "") or "").strip()
            if not identity:
                continue
            if identity.lower() == self.unknown_label:
                continue
            if not bool(attrs.get("identity_enabled", False)):
                continue
            try:
                similarity = float(attrs.get("similarity", 0.0) or 0.0)
            except (TypeError, ValueError):
                similarity = 0.0
            if similarity <= 0.0:
                continue
            identities.add(identity)
        return identities

    def _capture_snapshot(
        self,
        alarm_id: int,
        identity: str,
        timestamp_ms: int,
        frame_rgb: np.ndarray,
    ) -> dict[str, object] | None:
        rel_path = self._build_snapshot_relative_path(identity=identity, alarm_id=alarm_id, timestamp_ms=timestamp_ms)
        full_path = (self.image_root / rel_path).resolve()
        try:
            full_path.relative_to(self.image_root)
        except ValueError:
            return None

        try:
            saved = self._frame_saver(frame_rgb, full_path)
        except Exception:
            LOGGER.exception("face alarm snapshot save failed: %s", full_path)
            saved = False
        if not saved:
            return None

        snapshot = AlarmSnapshot(timestamp_ms=timestamp_ms, image_rel_path=rel_path.as_posix())
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO alarm_snapshots (alarm_id, timestamp_ms, image_rel_path)
                    VALUES (?, ?, ?)
                    """,
                    (int(alarm_id), int(timestamp_ms), snapshot.image_rel_path),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT alarm_id, source_id, identity FROM alarms WHERE alarm_id = ?",
                    (int(alarm_id),),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "alarm_id": int(row["alarm_id"]),
                    "identity": str(row["identity"]),
                    "source_id": str(row["source_id"]),
                    "snapshot": snapshot.as_dict(),
                }

    def _build_snapshot_relative_path(self, identity: str, alarm_id: int, timestamp_ms: int) -> Path:
        day = datetime.fromtimestamp(float(timestamp_ms) / 1000.0).strftime("%Y%m%d")
        safe_identity = (
            str(identity or "target")
            .strip()
            .replace("\\", "_")
            .replace("/", "_")
            .replace(":", "_")
        )
        if not safe_identity:
            safe_identity = "target"
        filename = f"{timestamp_ms}_{int(alarm_id)}.jpg"
        return Path(day) / safe_identity[:64] / filename

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.metadata_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_storage(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alarms (
                    alarm_id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    appeared_at_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    disappeared_at_ms INTEGER NULL
                );

                CREATE TABLE IF NOT EXISTS alarm_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alarm_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    image_rel_path TEXT NOT NULL,
                    FOREIGN KEY(alarm_id) REFERENCES alarms(alarm_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_alarm_snapshots_alarm_id ON alarm_snapshots(alarm_id);
                CREATE INDEX IF NOT EXISTS idx_alarms_appeared_at_ms ON alarms(appeared_at_ms DESC);
                """
            )
            # Finalize any dangling alarms from a previous process so history stays consistent across restarts.
            conn.execute(
                """
                UPDATE alarms
                SET disappeared_at_ms = last_seen_ms
                WHERE disappeared_at_ms IS NULL
                """
            )
            conn.commit()

    def _load_next_alarm_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(alarm_id), 0) AS max_alarm_id FROM alarms").fetchone()
            return int((row["max_alarm_id"] if row is not None else 0) or 0) + 1

    def _insert_alarm(self, record: AlarmRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alarms (alarm_id, source_id, identity, appeared_at_ms, last_seen_ms, disappeared_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(record.alarm_id),
                    record.source_id,
                    record.identity,
                    int(record.appeared_at_ms),
                    int(record.last_seen_ms),
                    record.disappeared_at_ms,
                ),
            )
            conn.commit()

    def _update_alarm_last_seen(self, alarm_id: int, last_seen_ms: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE alarms SET last_seen_ms = ? WHERE alarm_id = ?",
                (int(last_seen_ms), int(alarm_id)),
            )
            conn.commit()

    def _update_alarm_end(self, alarm_id: int, disappeared_at_ms: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE alarms SET disappeared_at_ms = ?, last_seen_ms = MAX(last_seen_ms, ?) WHERE alarm_id = ?",
                (int(disappeared_at_ms), int(disappeared_at_ms), int(alarm_id)),
            )
            conn.commit()

    def _fetch_alarm_dicts(self, conn: sqlite3.Connection, limit: int, active_only: bool) -> list[dict[str, object]]:
        where_clause = "WHERE disappeared_at_ms IS NULL" if active_only else ""
        rows = conn.execute(
            f"""
            SELECT alarm_id, source_id, identity, appeared_at_ms, last_seen_ms, disappeared_at_ms
            FROM alarms
            {where_clause}
            ORDER BY appeared_at_ms DESC, alarm_id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        now_ms = int(time.time() * 1000.0)
        return [self._row_to_record(conn, row).as_dict(now_ms=now_ms) for row in rows]

    def _row_to_record(self, conn: sqlite3.Connection, row: sqlite3.Row) -> AlarmRecord:
        snapshots = [
            AlarmSnapshot(timestamp_ms=int(item["timestamp_ms"]), image_rel_path=str(item["image_rel_path"]))
            for item in conn.execute(
                """
                SELECT timestamp_ms, image_rel_path
                FROM alarm_snapshots
                WHERE alarm_id = ?
                ORDER BY timestamp_ms ASC, id ASC
                """,
                (int(row["alarm_id"]),),
            ).fetchall()
        ]
        return AlarmRecord(
            alarm_id=int(row["alarm_id"]),
            source_id=str(row["source_id"]),
            identity=str(row["identity"]),
            appeared_at_ms=int(row["appeared_at_ms"]),
            last_seen_ms=int(row["last_seen_ms"]),
            disappeared_at_ms=int(row["disappeared_at_ms"]) if row["disappeared_at_ms"] is not None else None,
            snapshots=snapshots,
        )

    def _close_active_locked(self, now_ms: int) -> list[tuple[str, dict[str, object]]]:
        publish_items: list[tuple[str, dict[str, object]]] = []
        for identity in list(self._active.keys()):
            active = self._active.pop(identity)
            if active.record.disappeared_at_ms is None:
                active.record.disappeared_at_ms = now_ms
                self._update_alarm_end(active.record.alarm_id, disappeared_at_ms=now_ms)
                publish_items.append(("alarm.face.end", active.record.as_dict(now_ms=now_ms)))
        return publish_items

    def _publish(self, topic: str, payload: dict[str, object]) -> None:
        if self.event_publisher is None:
            return
        try:
            self.event_publisher.publish(topic=topic, source_id=self.source_id, payload=payload)
        except Exception:
            LOGGER.exception("face alarm event publish failed: %s", topic)
