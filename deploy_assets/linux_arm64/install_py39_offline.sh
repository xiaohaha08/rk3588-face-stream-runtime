#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${ROOT_DIR}/../.." && pwd)"
WHEEL_DIR="${ROOT_DIR}/python_wheels"
OFFLINE_DIR="${WHEEL_DIR}/py39_offline"
RUNTIME_LIB="${ROOT_DIR}/rknn_runtime/librknnrt.so"
WITH_API=0

if [[ "${1:-}" == "--with-api" ]]; then
  WITH_API=1
fi

echo "[1/4] 检查 Python 版本"
python3 - <<'PY'
import platform
import sys

print("python:", sys.version.split()[0])
print("machine:", platform.machine())
if not sys.version.startswith("3.9."):
    raise SystemExit("当前脚本只适用于 Python 3.9.x")
if platform.machine() != "aarch64":
    raise SystemExit("当前脚本只适用于 aarch64")
PY

echo "[2/4] 安装 Python 离线依赖"
python3 -m pip install --no-index \
  --find-links "${OFFLINE_DIR}" \
  --find-links "${WHEEL_DIR}" \
  numpy \
  opencv-python-headless \
  psutil \
  ruamel.yaml \
  rknn-toolkit-lite2

if [[ "${WITH_API}" -eq 1 ]]; then
  echo "[2.1/4] 安装 API 扩展依赖"
  python3 -m pip install --no-index \
    --find-links "${OFFLINE_DIR}" \
    importlib-metadata \
    zipp \
    Flask \
    simple-websocket
else
  echo "[2.1/4] 跳过 API 扩展依赖，当前仅安装阶段二/三所需核心包"
  echo "         如果后续需要启动 Flask API，可重新执行："
  echo "         ${BASH_SOURCE[0]} --with-api"
fi

echo "[3/4] 提示安装 librknnrt.so"
echo "请执行以下命令安装 RKNN runtime："
echo "  sudo cp '${RUNTIME_LIB}' /usr/lib/"
echo "  sudo ldconfig"

echo "[4/4] 验证导入"
python3 - <<'PY'
import cv2
from rknnlite.api import RKNNLite

print("cv2 ok:", cv2.__version__)
print("rknnlite ok:", RKNNLite)
PY

echo
echo "离线 Python 包安装完成。"
echo "当前工程目录：${PROJECT_DIR}"
