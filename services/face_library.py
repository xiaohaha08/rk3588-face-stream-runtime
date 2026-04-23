"""SQLite-backed face library management and embedding storage."""

from __future__ import annotations

import base64
import binascii
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from adapters.face_detector import FaceDetectorAdapter
from adapters.face_embedding import FaceEmbeddingAdapter, normalize_embedding
from services.config import DetectorConfig, GalleryConfig, RecognizerConfig


LOGGER = logging.getLogger(__name__)

DEFAULT_FACE_LIBRARY_DB = "face_library.db"
DEFAULT_FACE_SAMPLE_DIR = "face_library_samples"


def decode_image_base64(payload: str) -> bytes:
    """Decode plain base64 or data-url formatted image payload."""

    text = str(payload or "").strip()
    if not text:
        raise ValueError("image payload is required")
    if text.startswith("data:"):
        comma_index = text.find(",")
        if comma_index < 0:
            raise ValueError("invalid image data URL")
        text = text[comma_index + 1 :]
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image payload") from exc


def _now_iso_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class FaceLibraryPaths:
    root_dir: Path
    database_path: Path
    sample_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "root_dir": str(self.root_dir),
            "database_path": str(self.database_path),
            "sample_dir": str(self.sample_dir),
        }


class FaceLibraryService:
    """CRUD service for identity and sample management."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()

    def resolve_paths(self, gallery_config: GalleryConfig) -> FaceLibraryPaths:
        root_dir = Path(str(gallery_config.directory or "gallery").strip() or "gallery").expanduser()
        if not root_dir.is_absolute():
            root_dir = (self.project_root / root_dir).resolve()

        database_value = str(getattr(gallery_config, "database", "") or "").strip()
        if database_value:
            database_path = Path(database_value).expanduser()
            if not database_path.is_absolute():
                database_path = (self.project_root / database_path).resolve()
        else:
            database_path = (root_dir / DEFAULT_FACE_LIBRARY_DB).resolve()

        samples_subdir = str(getattr(gallery_config, "samples_subdir", "") or "").strip() or DEFAULT_FACE_SAMPLE_DIR
        sample_dir_candidate = Path(samples_subdir).expanduser()
        if sample_dir_candidate.is_absolute():
            sample_dir = sample_dir_candidate.resolve()
        else:
            sample_dir = (root_dir / sample_dir_candidate).resolve()

        return FaceLibraryPaths(
            root_dir=root_dir,
            database_path=database_path,
            sample_dir=sample_dir,
        )

    def list_identities(self, gallery_config: GalleryConfig, keyword: str = "") -> dict[str, object]:
        paths = self.resolve_paths(gallery_config)
        rows: list[dict[str, object]] = []
        identity_count = 0
        enabled_count = 0
        sample_total = 0
        last_updated_at = ""
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            term = str(keyword or "").strip().lower()
            like_term = f"%{term}%"
            result = conn.execute(
                """
                SELECT
                    i.id,
                    i.name,
                    i.note,
                    i.enabled,
                    i.created_at,
                    i.updated_at,
                    COUNT(s.id) AS sample_count,
                    (
                        SELECT s2.id
                        FROM samples s2
                        WHERE s2.identity_id = i.id
                        ORDER BY s2.created_at DESC, s2.id DESC
                        LIMIT 1
                    ) AS latest_sample_id,
                    (
                        SELECT s2.image_path
                        FROM samples s2
                        WHERE s2.identity_id = i.id
                        ORDER BY s2.created_at DESC, s2.id DESC
                        LIMIT 1
                    ) AS latest_sample_path
                FROM identities i
                LEFT JOIN samples s ON s.identity_id = i.id
                WHERE (? = '' OR lower(i.name) LIKE ? OR lower(i.note) LIKE ?)
                GROUP BY i.id
                ORDER BY i.updated_at DESC, i.id DESC
                """,
                (term, like_term, like_term),
            )
            for row in result.fetchall():
                sample_count = int(row["sample_count"] or 0)
                updated_at = str(row["updated_at"])
                rows.append(
                    {
                        "id": int(row["id"]),
                        "name": str(row["name"]),
                        "note": str(row["note"] or ""),
                        "enabled": bool(row["enabled"]),
                        "sample_count": sample_count,
                        "created_at": str(row["created_at"]),
                        "updated_at": updated_at,
                        "latest_sample_id": int(row["latest_sample_id"] or 0),
                        "latest_sample_path": str(row["latest_sample_path"] or ""),
                    }
                )
            summary_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS identity_count,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled_count,
                    COALESCE(MAX(updated_at), '') AS last_updated_at
                FROM identities
                """
            ).fetchone()
            if summary_row is not None:
                identity_count = int(summary_row["identity_count"] or 0)
                enabled_count = int(summary_row["enabled_count"] or 0)
                last_updated_at = str(summary_row["last_updated_at"] or "")
            sample_row = conn.execute("SELECT COUNT(*) AS sample_count FROM samples").fetchone()
            if sample_row is not None:
                sample_total = int(sample_row["sample_count"] or 0)
        db_exists = paths.database_path.is_file()
        db_size = 0
        db_modified_at = ""
        if db_exists:
            try:
                stat_result = paths.database_path.stat()
                db_size = int(stat_result.st_size)
                db_modified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat_result.st_mtime))
            except OSError:
                LOGGER.warning("failed to read database stat: %s", paths.database_path, exc_info=True)

        return {
            "paths": paths.as_dict(),
            "summary": {
                "database_exists": db_exists,
                "database_size_bytes": db_size,
                "database_modified_at": db_modified_at,
                "identity_count": identity_count,
                "enabled_identity_count": enabled_count,
                "sample_count": sample_total,
                "last_identity_update_at": last_updated_at,
            },
            "items": rows,
            "total": len(rows),
        }

    def create_identity(self, gallery_config: GalleryConfig, name: str, note: str = "", enabled: bool = True) -> dict[str, object]:
        identity_name = str(name or "").strip()
        if not identity_name:
            raise ValueError("identity name is required")
        paths = self.resolve_paths(gallery_config)
        now = _now_iso_utc()
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO identities (name, note, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (identity_name, str(note or "").strip(), 1 if enabled else 0, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"identity already exists: {identity_name}") from exc
            identity_id = int(cursor.lastrowid or 0)
            conn.commit()
            created = self._get_identity_row(conn, identity_id)
            if not created:
                raise RuntimeError("failed to fetch inserted identity")
            return created

    def update_identity(
        self,
        gallery_config: GalleryConfig,
        identity_id: int,
        name: str | None = None,
        note: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, object]:
        paths = self.resolve_paths(gallery_config)
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            current = self._get_identity_row(conn, identity_id)
            if not current:
                raise KeyError(f"identity not found: {identity_id}")

            next_name = str(name).strip() if name is not None else str(current["name"])
            next_note = str(note).strip() if note is not None else str(current["note"] or "")
            next_enabled = bool(enabled) if enabled is not None else bool(current["enabled"])
            if not next_name:
                raise ValueError("identity name is required")

            try:
                conn.execute(
                    """
                    UPDATE identities
                    SET name = ?, note = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_name, next_note, 1 if next_enabled else 0, _now_iso_utc(), int(identity_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"identity already exists: {next_name}") from exc
            conn.commit()
            updated = self._get_identity_row(conn, identity_id)
            if not updated:
                raise KeyError(f"identity not found: {identity_id}")
            return updated

    def delete_identity(self, gallery_config: GalleryConfig, identity_id: int) -> dict[str, object]:
        paths = self.resolve_paths(gallery_config)
        image_paths: list[Path] = []
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            current = self._get_identity_row(conn, identity_id)
            if not current:
                raise KeyError(f"identity not found: {identity_id}")
            sample_rows = conn.execute(
                "SELECT image_path FROM samples WHERE identity_id = ?",
                (int(identity_id),),
            ).fetchall()
            image_paths = [self._resolve_sample_path(paths, str(row["image_path"])) for row in sample_rows]
            conn.execute("DELETE FROM identities WHERE id = ?", (int(identity_id),))
            conn.commit()

        deleted_files = 0
        for image_path in image_paths:
            try:
                if image_path.is_file():
                    image_path.unlink()
                    deleted_files += 1
            except OSError:
                LOGGER.warning("failed to remove sample image: %s", image_path, exc_info=True)

        return {
            "id": int(current["id"]),
            "name": str(current["name"]),
            "deleted_samples": len(image_paths),
            "deleted_files": deleted_files,
        }

    def list_samples(self, gallery_config: GalleryConfig, identity_id: int) -> dict[str, object]:
        paths = self.resolve_paths(gallery_config)
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            identity = self._get_identity_row(conn, identity_id)
            if not identity:
                raise KeyError(f"identity not found: {identity_id}")
            rows = conn.execute(
                """
                SELECT id, identity_id, image_path, embedding_dim, face_score, created_at
                FROM samples
                WHERE identity_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (int(identity_id),),
            ).fetchall()
            items = [self._sample_row_to_dict(paths, row) for row in rows]
            return {
                "identity": identity,
                "paths": paths.as_dict(),
                "items": items,
                "total": len(items),
            }

    def add_sample_from_image_bytes(
        self,
        gallery_config: GalleryConfig,
        identity_id: int,
        image_bytes: bytes,
        detector_model_path: str,
        recognizer_model_path: str,
        detector_config: DetectorConfig,
        recognizer_config: RecognizerConfig,
        core_id: int = 0,
    ) -> dict[str, object]:
        if not image_bytes:
            raise ValueError("empty image payload")

        paths = self.resolve_paths(gallery_config)
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            identity = self._get_identity_row(conn, identity_id)
            if not identity:
                raise KeyError(f"identity not found: {identity_id}")

        decoded = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("failed to decode image data")
        frame_rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)

        detector = FaceDetectorAdapter(
            model_path=str(detector_model_path),
            config=detector_config,
            core_id=int(core_id),
        )
        embedder = FaceEmbeddingAdapter(
            model_path=str(recognizer_model_path),
            config=recognizer_config,
            core_id=int(core_id),
        )
        try:
            faces = detector.detect(frame_rgb)
            if not faces:
                raise ValueError("no face detected in image")
            best_face = max(faces, key=lambda item: item.score)
            embedding = embedder.extract(frame_rgb, best_face.bbox)
            if embedding is None:
                raise ValueError("failed to extract face embedding")
            normalized = normalize_embedding(embedding)
        finally:
            detector.close()
            embedder.close()

        paths.sample_dir.mkdir(parents=True, exist_ok=True)
        image_name = f"{int(identity_id)}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.jpg"
        image_path = (paths.sample_dir / image_name).resolve()
        ok, encoded = cv2.imencode(".jpg", decoded, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise ValueError("failed to encode sample image")
        image_path.write_bytes(encoded.tobytes())
        stored_path = self._store_relative_path(paths, image_path)
        now = _now_iso_utc()

        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                """
                INSERT INTO samples (
                    identity_id,
                    image_path,
                    embedding_blob,
                    embedding_dim,
                    face_score,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(identity_id),
                    stored_path,
                    normalized.astype(np.float32).tobytes(),
                    int(normalized.size),
                    float(best_face.score),
                    now,
                ),
            )
            conn.execute(
                "UPDATE identities SET updated_at = ? WHERE id = ?",
                (now, int(identity_id)),
            )
            sample_id = int(cursor.lastrowid or 0)
            conn.commit()
            row = conn.execute(
                """
                SELECT id, identity_id, image_path, embedding_dim, face_score, created_at
                FROM samples
                WHERE id = ?
                """,
                (sample_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("failed to fetch inserted sample")
            return self._sample_row_to_dict(paths, row)

    def delete_sample(self, gallery_config: GalleryConfig, sample_id: int) -> dict[str, object]:
        paths = self.resolve_paths(gallery_config)
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                """
                SELECT id, identity_id, image_path
                FROM samples
                WHERE id = ?
                """,
                (int(sample_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"sample not found: {sample_id}")
            image_path = self._resolve_sample_path(paths, str(row["image_path"]))
            identity_id = int(row["identity_id"])
            conn.execute("DELETE FROM samples WHERE id = ?", (int(sample_id),))
            conn.execute(
                "UPDATE identities SET updated_at = ? WHERE id = ?",
                (_now_iso_utc(), identity_id),
            )
            conn.commit()

        file_deleted = False
        try:
            if image_path.is_file():
                image_path.unlink()
                file_deleted = True
        except OSError:
            LOGGER.warning("failed to remove sample image: %s", image_path, exc_info=True)

        return {
            "id": int(sample_id),
            "identity_id": identity_id,
            "file_deleted": file_deleted,
        }

    def get_sample_image_path(self, gallery_config: GalleryConfig, sample_id: int) -> Path | None:
        paths = self.resolve_paths(gallery_config)
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                """
                SELECT image_path
                FROM samples
                WHERE id = ?
                """,
                (int(sample_id),),
            ).fetchone()
            if row is None:
                return None
            candidate = self._resolve_sample_path(paths, str(row["image_path"]))
            if not candidate.is_file():
                return None
            return candidate

    def load_embeddings(self, gallery_config: GalleryConfig, enabled_only: bool = False) -> dict[str, np.ndarray]:
        paths = self.resolve_paths(gallery_config)
        if not paths.database_path.is_file():
            return {}
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT i.name, i.enabled, s.embedding_blob, s.embedding_dim
                FROM samples s
                JOIN identities i ON i.id = s.identity_id
                ORDER BY s.id ASC
                """
            ).fetchall()

        grouped: dict[str, list[np.ndarray]] = {}
        for row in rows:
            name = str(row["name"] or "").strip()
            if not name:
                continue
            if enabled_only and not bool(row["enabled"]):
                continue
            vector = np.frombuffer(row["embedding_blob"], dtype=np.float32)
            dim = int(row["embedding_dim"] or 0)
            if dim <= 0 or vector.size == 0:
                continue
            if vector.size != dim:
                vector = vector[:dim]
            if vector.size != dim:
                continue
            grouped.setdefault(name, []).append(normalize_embedding(vector))

        embeddings: dict[str, np.ndarray] = {}
        for name, vectors in grouped.items():
            if not vectors:
                continue
            mean_vector = np.mean(np.stack(vectors, axis=0), axis=0)
            embeddings[name] = normalize_embedding(mean_vector)
        return embeddings

    def load_identity_statuses(self, gallery_config: GalleryConfig) -> dict[str, bool]:
        paths = self.resolve_paths(gallery_config)
        if not paths.database_path.is_file():
            return {}
        with self._connect(paths.database_path) as conn:
            self._ensure_schema(conn)
            rows = conn.execute("SELECT name, enabled FROM identities ORDER BY id ASC").fetchall()
        return {str(row["name"] or "").strip(): bool(row["enabled"]) for row in rows if str(row["name"] or "").strip()}

    def _connect(self, database_path: Path) -> sqlite3.Connection:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                note TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_id INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                embedding_blob BLOB NOT NULL,
                embedding_dim INTEGER NOT NULL,
                face_score REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(identity_id) REFERENCES identities(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_samples_identity_id ON samples(identity_id);
            """
        )
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(identities)").fetchall()}
        if "enabled" not in columns:
            conn.execute("ALTER TABLE identities ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
            conn.commit()

    def _get_identity_row(self, conn: sqlite3.Connection, identity_id: int) -> dict[str, object] | None:
        row = conn.execute(
            """
            SELECT
                i.id,
                i.name,
                i.note,
                i.enabled,
                i.created_at,
                i.updated_at,
                (
                    SELECT COUNT(*)
                    FROM samples s
                    WHERE s.identity_id = i.id
                ) AS sample_count
            FROM identities i
            WHERE i.id = ?
            """,
            (int(identity_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "note": str(row["note"] or ""),
            "enabled": bool(row["enabled"]),
            "sample_count": int(row["sample_count"] or 0),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _sample_row_to_dict(self, paths: FaceLibraryPaths, row: sqlite3.Row) -> dict[str, object]:
        sample_path_text = str(row["image_path"])
        sample_path = self._resolve_sample_path(paths, sample_path_text)
        return {
            "id": int(row["id"]),
            "identity_id": int(row["identity_id"]),
            "image_path": sample_path_text,
            "embedding_dim": int(row["embedding_dim"] or 0),
            "face_score": float(row["face_score"] or 0.0),
            "created_at": str(row["created_at"]),
            "image_exists": sample_path.is_file(),
        }

    def _store_relative_path(self, paths: FaceLibraryPaths, sample_path: Path) -> str:
        try:
            relative = sample_path.resolve().relative_to(paths.root_dir.resolve())
            return str(relative).replace("\\", "/")
        except ValueError:
            return str(sample_path.resolve())

    def _resolve_sample_path(self, paths: FaceLibraryPaths, sample_path: str) -> Path:
        candidate = Path(str(sample_path or "").strip()).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (paths.root_dir / candidate).resolve()
