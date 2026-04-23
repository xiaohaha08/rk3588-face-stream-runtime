"""Logging helpers for the stage-1 runtime."""

from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once per process."""
    resolved = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
