"""Lightweight mediamtx process wrapper for board-side stream management."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class MediaMtxConfig:
    binary_path: str
    config_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8554
    start_timeout_ms: int = 5000
    retry_interval_ms: int = 200


def is_tcp_port_open(host: str, port: int, timeout_seconds: float = 0.5) -> bool:
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def wait_for_tcp_port(
    host: str,
    port: int,
    timeout_ms: int = 5000,
    retry_interval_ms: int = 200,
) -> bool:
    deadline = time.perf_counter() + max(0.1, float(timeout_ms) / 1000.0)
    interval = max(0.05, float(retry_interval_ms) / 1000.0)
    while time.perf_counter() < deadline:
        if is_tcp_port_open(host=host, port=port):
            return True
        time.sleep(interval)
    return is_tcp_port_open(host=host, port=port)


class MediaMtxProcess:
    def __init__(self, config: MediaMtxConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None

    def build_command(self) -> list[str]:
        command = [str(os.path.expanduser(self.config.binary_path))]
        if str(self.config.config_path or "").strip():
            command.append(str(os.path.expanduser(self.config.config_path)))
        return command

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._process = subprocess.Popen(
            self.build_command(),
            stdout=None,
            stderr=None,
            stdin=subprocess.DEVNULL,
        )

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def is_ready(self) -> bool:
        return is_tcp_port_open(self.config.host, self.config.port)

    def wait_until_ready(self) -> bool:
        return wait_for_tcp_port(
            host=self.config.host,
            port=self.config.port,
            timeout_ms=self.config.start_timeout_ms,
            retry_interval_ms=self.config.retry_interval_ms,
        )

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
