# 部署资源说明

`deploy_assets/` 只保留正式部署需要的运行资源，不包含测试资产、旧工程代码或阶段性文档。

以下说明默认都在仓库根目录执行。

## 目录内容

- `linux_arm64/mediamtx/`
  RK3588 Linux ARM64 环境下使用的 MediaMTX 二进制和配置文件。

- `linux_arm64/ffmpeg/`
  RK3588 对应的 FFmpeg 资源包。

- `linux_arm64/rknn_runtime/`
  RKNN Runtime 动态库资源。

- `linux_arm64/python_wheels/`
  Python 3.9 离线安装 wheels，包括 RKNNLite 与控制面依赖。

- `windows_amd64/mediamtx/`
  Windows 开发机本地联调 RTSP 时可用的 MediaMTX。

## 推荐使用顺序

### 1. 安装 RKNN Runtime

```bash
sudo cp deploy_assets/linux_arm64/rknn_runtime/librknnrt.so /usr/lib/
sudo ldconfig
```

如果系统使用 `aarch64-linux-gnu` 目录：

```bash
sudo cp deploy_assets/linux_arm64/rknn_runtime/librknnrt.so /usr/lib/aarch64-linux-gnu/
sudo ldconfig
```

### 2. 安装 Python 依赖

```bash
chmod +x deploy_assets/linux_arm64/install_py39_offline.sh
./deploy_assets/linux_arm64/install_py39_offline.sh --with-api
```

更详细说明见：

- `deploy_assets/linux_arm64/INSTALL_PY39_OFFLINE.md`

### 3. 准备运行目录

```bash
mkdir -p gallery video/input video/output outputs/face_alarm_images
```

### 4. 启动工程

视频运行时：

```bash
python3 scripts/run_video_runtime.py --config configs/usb_camera_rtsp.json --timeout 0
```

控制面：

```bash
python3 scripts/run_control_plane.py --config configs/usb_camera_rtsp.json
```

## 使用建议

- 优先在 RK3588 Linux ARM64 环境运行主链路
- 如果要输出 RTSP，请先确认 `mediamtx` 可用
- 如果 `ffmpeg` 不在系统 `PATH` 中，请在配置里填写明确路径
- 本地视频场景统一使用仓库内相对路径：
  - `video/input`
  - `video/output`
- 控制面默认地址由配置中的 `api.host` 和 `api.port` 决定

## 相关文档

- 根文档：`README.md`
- Python 离线安装：`deploy_assets/linux_arm64/INSTALL_PY39_OFFLINE.md`
