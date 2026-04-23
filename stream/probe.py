"""Stream probing helpers built on top of ffprobe."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from stream.common import is_rtsp_url, kbps_from_bits, parse_fps


@dataclass(frozen=True)
class StreamInfo:
    width: int = 0
    height: int = 0
    fps: float = 25.0
    codec: str = ""
    bitrate_kbps: int = 0
    pix_fmt: str = ""
    duration_seconds: float = 0.0


def probe_stream_info(
    input_url: str,
    ffprobe_path: str = "ffprobe",
    transport: str = "tcp",
) -> StreamInfo:
    command = [str(ffprobe_path or "ffprobe"), "-v", "error"]
    if is_rtsp_url(input_url):
        command += ["-rtsp_transport", str(transport or "tcp")]
    command += [
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt,avg_frame_rate,bit_rate:format=bit_rate,duration",
        "-of",
        "default=noprint_wrappers=1",
        str(input_url),
    ]

    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
        )
    except FileNotFoundError:
        return StreamInfo()
    except Exception:
        return StreamInfo()

    if process.returncode != 0:
        return StreamInfo()

    parsed: dict[str, str] = {}
    for line in process.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()

    bitrate = kbps_from_bits(parsed.get("bit_rate", "0"))
    if bitrate <= 0:
        bitrate = kbps_from_bits(parsed.get("TAG:bit_rate", "0"))
    try:
        duration = float(parsed.get("duration", "0") or 0.0)
    except ValueError:
        duration = 0.0

    return StreamInfo(
        width=int(parsed.get("width", "0") or 0),
        height=int(parsed.get("height", "0") or 0),
        fps=parse_fps(parsed.get("avg_frame_rate", "")) or 25.0,
        codec=str(parsed.get("codec_name", "") or "").lower(),
        bitrate_kbps=bitrate,
        pix_fmt=str(parsed.get("pix_fmt", "") or "").lower(),
        duration_seconds=max(0.0, duration),
    )
