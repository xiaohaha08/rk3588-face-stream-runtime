"""板端系统信息采集，供控制面展示 CPU / 内存 / NPU / 温度等状态。"""

from __future__ import annotations

import os
import re
import socket
import time
from pathlib import Path
from typing import Any


class SystemMonitor:
    """Best-effort 系统监控。

    设计目标：
    - 优先兼容 RK3588 板端常见路径
    - 读不到时返回 None，而不是抛异常
    - 不阻塞主推理链，控制面按需拉取
    """

    _PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
    _NUMBER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")

    def __init__(
        self,
        *,
        thermal_root: str | Path = "/sys/class/thermal",
        devfreq_root: str | Path = "/sys/class/devfreq",
        debug_candidates: tuple[str | Path, ...] = (
            "/sys/kernel/debug/rknpu/load",
            "/sys/kernel/debug/rknpu/utilization",
        ),
        psutil_module: Any | None = None,
    ) -> None:
        self.thermal_root = Path(thermal_root)
        self.devfreq_root = Path(devfreq_root)
        self.debug_candidates = tuple(Path(item) for item in debug_candidates)
        self.psutil = psutil_module
        if self.psutil is None:
            try:
                import psutil as _psutil  # type: ignore
            except Exception:  # pragma: no cover - optional on dev host
                _psutil = None
            self.psutil = _psutil

    def snapshot(self) -> dict[str, Any]:
        thermal = self._thermal_snapshot()
        cpu = self._cpu_snapshot(thermal=thermal)
        memory = self._memory_snapshot()
        npu = self._npu_snapshot(thermal=thermal)
        return {
            "timestamp_ms": int(time.time() * 1000),
            "hostname": self._hostname(),
            "ip": self._ip_address(),
            "cpu": cpu,
            "memory": memory,
            "npu": npu,
            "gpu": {
                "temperature_c": thermal.get("gpu_temp_c"),
            },
            "thermal": thermal,
        }

    def _cpu_snapshot(self, *, thermal: dict[str, Any]) -> dict[str, Any]:
        percent = None
        logical = None
        physical = None
        load_avg = {"load1": None, "load5": None, "load15": None}
        if self.psutil is not None:
            try:
                percent = round(float(self.psutil.cpu_percent(interval=None)), 1)
            except Exception:
                percent = None
            try:
                logical = int(self.psutil.cpu_count(logical=True) or 0)
            except Exception:
                logical = None
            try:
                physical = int(self.psutil.cpu_count(logical=False) or 0)
            except Exception:
                physical = None
        try:
            load1, load5, load15 = os.getloadavg()
            load_avg = {
                "load1": round(float(load1), 2),
                "load5": round(float(load5), 2),
                "load15": round(float(load15), 2),
            }
        except Exception:
            pass
        return {
            "percent": percent,
            "count_logical": logical,
            "count_physical": physical,
            "temperature_c": thermal.get("cpu_temp_c"),
            **load_avg,
        }

    def _memory_snapshot(self) -> dict[str, Any]:
        if self.psutil is not None:
            try:
                mem = self.psutil.virtual_memory()
                used_mb = int((mem.total - mem.available) / (1024 * 1024))
                total_mb = int(mem.total / (1024 * 1024))
                return {
                    "percent": round(float(mem.percent), 1),
                    "used_mb": used_mb,
                    "total_mb": total_mb,
                    "available_mb": int(mem.available / (1024 * 1024)),
                }
            except Exception:
                pass
        meminfo = self._read_meminfo()
        total_kb = meminfo.get("MemTotal")
        available_kb = meminfo.get("MemAvailable")
        if total_kb is None or available_kb is None or total_kb <= 0:
            return {"percent": None, "used_mb": None, "total_mb": None, "available_mb": None}
        used_kb = max(0, total_kb - available_kb)
        return {
            "percent": round((used_kb / total_kb) * 100.0, 1),
            "used_mb": int(used_kb / 1024),
            "total_mb": int(total_kb / 1024),
            "available_mb": int(available_kb / 1024),
        }

    def _npu_snapshot(self, *, thermal: dict[str, Any]) -> dict[str, Any]:
        usage = self._read_npu_usage()
        freq = self._read_npu_frequency()
        return {
            "percent": usage.get("percent"),
            "per_core_percent": usage.get("per_core_percent", []),
            "source": usage.get("source", ""),
            "temperature_c": thermal.get("npu_temp_c"),
            "frequency_mhz": freq.get("current_mhz"),
            "max_frequency_mhz": freq.get("max_mhz"),
        }

    def _thermal_snapshot(self) -> dict[str, Any]:
        zones: list[dict[str, Any]] = []
        for zone_dir in sorted(self.thermal_root.glob("thermal_zone*")):
            temp_path = zone_dir / "temp"
            type_path = zone_dir / "type"
            if not temp_path.is_file():
                continue
            temp_c = self._read_temp_c(temp_path)
            zone_type = self._safe_read_text(type_path) or zone_dir.name
            zones.append(
                {
                    "zone": zone_dir.name,
                    "type": zone_type.strip(),
                    "temp_c": temp_c,
                }
            )
        cpu_temp = self._pick_temp(zones, keywords=("cpu", "little", "big"))
        gpu_temp = self._pick_temp(zones, keywords=("gpu",))
        npu_temp = self._pick_temp(zones, keywords=("npu",))
        if cpu_temp is None:
            cpu_temp = self._read_temp_c(self.thermal_root / "thermal_zone0" / "temp")
        if gpu_temp is None:
            gpu_temp = self._read_temp_c(self.thermal_root / "thermal_zone5" / "temp")
        if npu_temp is None:
            npu_temp = self._read_temp_c(self.thermal_root / "thermal_zone6" / "temp")
        return {
            "cpu_temp_c": cpu_temp,
            "gpu_temp_c": gpu_temp,
            "npu_temp_c": npu_temp,
            "zones": zones,
        }

    def _pick_temp(self, zones: list[dict[str, Any]], *, keywords: tuple[str, ...]) -> float | None:
        for item in zones:
            zone_type = str(item.get("type", "")).strip().lower()
            if any(keyword in zone_type for keyword in keywords):
                value = item.get("temp_c")
                return float(value) if isinstance(value, (int, float)) else None
        return None

    def _read_temp_c(self, path: Path) -> float | None:
        text = self._safe_read_text(path)
        if not text:
            return None
        try:
            raw = float(text.strip())
        except ValueError:
            return None
        if raw > 1000.0:
            raw /= 1000.0
        return round(raw, 1)

    def _read_npu_usage(self) -> dict[str, Any]:
        for path in self.debug_candidates:
            parsed = self._parse_usage_text(self._safe_read_text(path))
            if parsed["percent"] is not None:
                parsed["source"] = str(path)
                return parsed
        for devfreq_dir in sorted(self.devfreq_root.glob("*")):
            if "npu" not in devfreq_dir.name.lower():
                continue
            load_path = devfreq_dir / "load"
            parsed = self._parse_usage_text(self._safe_read_text(load_path))
            if parsed["percent"] is not None:
                parsed["source"] = str(load_path)
                return parsed
        return {"percent": None, "per_core_percent": [], "source": ""}

    def _read_npu_frequency(self) -> dict[str, Any]:
        for devfreq_dir in sorted(self.devfreq_root.glob("*")):
            if "npu" not in devfreq_dir.name.lower():
                continue
            cur_hz = self._read_int(devfreq_dir / "cur_freq")
            max_hz = self._read_int(devfreq_dir / "max_freq")
            if cur_hz is None and max_hz is None:
                continue
            return {
                "current_mhz": round(cur_hz / 1_000_000, 1) if cur_hz is not None else None,
                "max_mhz": round(max_hz / 1_000_000, 1) if max_hz is not None else None,
            }
        return {"current_mhz": None, "max_mhz": None}

    def _parse_usage_text(self, text: str | None) -> dict[str, Any]:
        if not text:
            return {"percent": None, "per_core_percent": []}
        percent_matches = [float(item) for item in self._PERCENT_PATTERN.findall(text)]
        if percent_matches:
            overall = round(sum(percent_matches) / len(percent_matches), 1)
            return {"percent": overall, "per_core_percent": [round(item, 1) for item in percent_matches]}
        number_matches = [float(item) for item in self._NUMBER_PATTERN.findall(text)]
        if not number_matches:
            return {"percent": None, "per_core_percent": []}
        if len(number_matches) >= 2 and number_matches[0] > 0 and number_matches[1] > 0 and "/" in text:
            ratio = (number_matches[0] / number_matches[1]) * 100.0
            return {"percent": round(ratio, 1), "per_core_percent": []}
        candidate = number_matches[-1]
        if candidate <= 100.0:
            return {"percent": round(candidate, 1), "per_core_percent": []}
        return {"percent": None, "per_core_percent": []}

    def _read_meminfo(self) -> dict[str, int]:
        result: dict[str, int] = {}
        meminfo_path = Path("/proc/meminfo")
        text = self._safe_read_text(meminfo_path)
        if not text:
            return result
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            match = self._NUMBER_PATTERN.search(value)
            if match:
                result[key.strip()] = int(float(match.group(1)))
        return result

    def _read_int(self, path: Path) -> int | None:
        text = self._safe_read_text(path)
        if not text:
            return None
        try:
            return int(float(text.strip()))
        except ValueError:
            return None

    def _hostname(self) -> str:
        try:
            return socket.gethostname()
        except Exception:
            return ""

    def _ip_address(self) -> str:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0])
            finally:
                sock.close()
        except Exception:
            return "127.0.0.1"

    def _safe_read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
