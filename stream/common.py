"""FFmpeg/RK3588 stream common helpers."""

from __future__ import annotations

from collections import deque
from typing import BinaryIO


def is_rtsp_url(value: str) -> bool:
    return str(value or "").strip().lower().startswith("rtsp://")


def parse_fps(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" not in text:
        try:
            return float(text)
        except ValueError:
            return 0.0
    left, right = text.split("/", 1)
    try:
        numerator = float(left)
        denominator = float(right)
    except ValueError:
        return 0.0
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def kbps_from_bits(value: str) -> int:
    try:
        bits = int(float(value))
    except (TypeError, ValueError):
        return 0
    if bits <= 0:
        return 0
    return max(1, bits // 1000)


def read_exact(pipe: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while received < size:
        block = pipe.read(size - received)
        if not block:
            break
        chunks.append(block)
        received += len(block)
    return b"".join(chunks)


def decoder_candidates(codec: str, preferred_decoder: str = "auto") -> list[str]:
    preferred = str(preferred_decoder or "auto").strip().lower()
    if preferred not in {"", "auto"}:
        return [preferred]

    normalized = str(codec or "").strip().lower()
    if normalized in {"h264", "avc"}:
        return ["h264_rkmpp", "h264"]
    if normalized in {"hevc", "h265"}:
        return ["hevc_rkmpp", "hevc"]
    return ["h264_rkmpp", "hevc_rkmpp", "h264", "hevc"]


def trim_tail(tail: deque[str], max_items: int = 8) -> str:
    if not tail:
        return ""
    return " | ".join(list(tail)[-max(1, int(max_items)):])
