# RK3588 Python 3.9 离线安装说明

适用环境：

- Linux `aarch64`
- Python `3.9.x`

## 1. 安装 RKNN runtime

```bash
sudo cp /path/to/workspace/deploy_assets/linux_arm64/rknn_runtime/librknnrt.so /usr/lib/
sudo ldconfig
```

如果系统使用 `aarch64-linux-gnu` 目录：

```bash
sudo cp /path/to/workspace/deploy_assets/linux_arm64/rknn_runtime/librknnrt.so /usr/lib/aarch64-linux-gnu/
sudo ldconfig
```

## 2. 离线安装 Python 依赖

```bash
python3 -m pip install --no-index \
  --find-links /path/to/workspace/deploy_assets/linux_arm64/python_wheels/py39_offline \
  --find-links /path/to/workspace/deploy_assets/linux_arm64/python_wheels \
  numpy \
  opencv-python-headless \
  psutil \
  ruamel.yaml \
  rknn-toolkit-lite2 \
  Flask \
  simple-websocket
```

也可以直接执行仓库内脚本：

```bash
chmod +x /path/to/workspace/deploy_assets/linux_arm64/install_py39_offline.sh
/path/to/workspace/deploy_assets/linux_arm64/install_py39_offline.sh --with-api
```

## 3. 验证安装

```bash
python3 - <<'PY'
import cv2
from rknnlite.api import RKNNLite

print("cv2 ok:", cv2.__version__)
print("rknnlite ok:", RKNNLite)
PY
```

## 4. 验证工程启动

运行视频主链路：

```bash
python3 /path/to/workspace/scripts/run_video_runtime.py \
  --config /path/to/workspace/configs/usb_camera_rtsp.json \
  --timeout 20
```

运行控制面：

```bash
python3 /path/to/workspace/scripts/run_control_plane.py \
  --config /path/to/workspace/configs/usb_camera_rtsp.json
```

## 5. 常见问题

### `No module named 'rknnlite'`

说明 `rknn_toolkit_lite2` 没有安装成功，重新执行离线安装命令。

### 找不到 `librknnrt.so`

检查以下路径之一是否存在：

```bash
ls /usr/lib/librknnrt.so
ls /usr/lib/aarch64-linux-gnu/librknnrt.so
```

### `cv2` 导入失败

重新安装 `opencv-python-headless`。

### Flask 无法启动

确认已经安装：

- `Flask`
- `simple-websocket`
