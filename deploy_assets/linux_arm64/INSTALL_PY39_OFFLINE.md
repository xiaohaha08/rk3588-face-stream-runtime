# RK3588 Python 3.9 离线安装说明

适用环境：

- Linux `aarch64`
- Python `3.9.x`

以下命令默认都在仓库根目录执行。

## 1. 安装 RKNN Runtime

```bash
sudo cp deploy_assets/linux_arm64/rknn_runtime/librknnrt.so /usr/lib/
sudo ldconfig
```

如果系统使用 `aarch64-linux-gnu` 目录：

```bash
sudo cp deploy_assets/linux_arm64/rknn_runtime/librknnrt.so /usr/lib/aarch64-linux-gnu/
sudo ldconfig
```

## 2. 离线安装 Python 依赖

推荐直接执行仓库内脚本：

```bash
chmod +x deploy_assets/linux_arm64/install_py39_offline.sh
./deploy_assets/linux_arm64/install_py39_offline.sh --with-api
```

如果需要手动安装：

```bash
python3 -m pip install --no-index \
  --find-links deploy_assets/linux_arm64/python_wheels/py39_offline \
  --find-links deploy_assets/linux_arm64/python_wheels \
  numpy \
  opencv-python-headless \
  psutil \
  ruamel.yaml \
  rknn-toolkit-lite2 \
  flask \
  simple-websocket
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

## 4. 准备运行目录

```bash
mkdir -p gallery video/input video/output outputs/face_alarm_images
```

## 5. 验证工程启动

运行视频主链路：

```bash
python3 scripts/run_video_runtime.py \
  --config configs/usb_camera_rtsp.json \
  --timeout 20
```

运行控制面：

```bash
python3 scripts/run_control_plane.py \
  --config configs/usb_camera_rtsp.json
```

## 6. 常见问题

### `No module named 'rknnlite'`

说明 `rknn-toolkit-lite2` 没有安装成功，请重新执行离线安装命令。

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

- `flask`
- `simple-websocket`
