"""Face gallery embedding store with database and directory fallbacks."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from adapters.face_detector import FaceDetectorAdapter
from adapters.face_embedding import FaceEmbeddingAdapter, normalize_embedding
from services.config import GalleryConfig
from services.face_library import FaceLibraryService


LOGGER = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _scan_gallery(root_dir: str, max_images_per_identity: int) -> dict[str, list[Path]]:
    root = Path(root_dir)
    if not root_dir or not root.is_dir():
        return {}

    grouped: dict[str, list[Path]] = {}
    subdirs = [item for item in root.iterdir() if item.is_dir()]
    if subdirs:
        for subdir in sorted(subdirs):
            images = [path for path in sorted(subdir.iterdir()) if path.suffix.lower() in IMAGE_EXTS]
            if images:
                grouped[subdir.name] = images[:max_images_per_identity]
        return grouped

    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        grouped.setdefault(path.stem, []).append(path)

    return {name: paths[:max_images_per_identity] for name, paths in grouped.items() if paths}


@dataclass
class GalleryStore:
    """In-memory identity embeddings used by the recognizer sidecar."""

    embeddings: dict[str, np.ndarray] = field(default_factory=dict)
    identity_enabled: dict[str, bool] = field(default_factory=dict)

    def identity_count(self) -> int:
        return len(self.embeddings)

    def is_enabled(self, identity: str) -> bool:
        name = str(identity or "").strip()
        if not name:
            return False
        if name not in self.embeddings:
            return False
        return bool(self.identity_enabled.get(name, True))

    def set_enabled(self, identity: str, enabled: bool) -> bool:
        name = str(identity or "").strip()
        if not name or name not in self.embeddings:
            return False
        self.identity_enabled[name] = bool(enabled)
        return True

    def match(self, embedding: np.ndarray, threshold: float, unknown_label: str) -> tuple[str, float]:
        if not self.embeddings:
            return unknown_label, 0.0

        vector = normalize_embedding(embedding)
        best_name = unknown_label
        best_score = -1.0
        for name, reference in self.embeddings.items():
            score = float(np.dot(vector, reference))
            if score > best_score:
                best_name = name
                best_score = score

        if best_score >= float(threshold):
            return best_name, best_score
        return unknown_label, max(0.0, best_score)

    @classmethod
    def load_from_directory(
        cls,
        gallery_config: GalleryConfig,
        detector: FaceDetectorAdapter,
        embedder: FaceEmbeddingAdapter,
    ) -> "GalleryStore":
        library_service = FaceLibraryService(project_root=PROJECT_ROOT)
        db_embeddings = library_service.load_embeddings(gallery_config, enabled_only=False)
        if db_embeddings:
            LOGGER.info("gallery identities loaded from sqlite: %s", len(db_embeddings))
            statuses = library_service.load_identity_statuses(gallery_config)
            return cls(embeddings=db_embeddings, identity_enabled={name: bool(statuses.get(name, True)) for name in db_embeddings})

        grouped = _scan_gallery(gallery_config.directory, max(1, int(gallery_config.max_images_per_identity)))
        if not grouped:
            LOGGER.info("gallery directory is empty or missing: %s", gallery_config.directory)
            return cls()

        embeddings: dict[str, np.ndarray] = {}
        for identity, image_paths in grouped.items():
            features: list[np.ndarray] = []
            for image_path in image_paths:
                bgr = cv2.imread(str(image_path))
                if bgr is None:
                    LOGGER.warning("failed to read gallery image: %s", image_path)
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                faces = detector.detect(rgb)
                if not faces:
                    LOGGER.warning("no face detected in gallery image: %s", image_path)
                    continue
                best_face = max(faces, key=lambda item: item.score)
                embedding = embedder.extract(rgb, best_face.bbox)
                if embedding is None:
                    LOGGER.warning("failed to extract embedding from gallery image: %s", image_path)
                    continue
                features.append(embedding)

            if not features:
                continue
            mean_feature = np.mean(np.stack(features, axis=0), axis=0)
            embeddings[identity] = normalize_embedding(mean_feature)

        LOGGER.info("gallery identities loaded from directory: %s", len(embeddings))
        return cls(embeddings=embeddings, identity_enabled={name: True for name in embeddings})
