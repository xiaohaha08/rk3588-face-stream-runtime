# 部署资源说明

`deploy_assets/` 只保留正式部署需要的运行资源，不包含测试资产、阶段性文档或旧工程代码。

## 目录内容

- `linux_arm64/mediamtx/`
  RK3588 Linux ARM64 环境下使用的 MediaMTX 资源。

- `linux_arm64/ffmpeg/`
  RK3588 对应的 FFmpeg 资源包。

- `linux_arm64/rknn_runtime/`
  RKNN runtime 动态库资源。

- `linux_arm64/python_wheels/`
  Python 3.9 离线安装 wheel，包括 RKNNLite 和依赖。

- `windows_amd64/mediamtx/`
  Windows 开发机本地调试 RTSP 时可用的 MediaMTX。

## 正式运行入口

视频运行时：

```bash
python scripts/run_video_runtime.py --config configs/usb_camera_rtsp.json
```

控制面：

```bash
python scripts/run_control_plane.py --config configs/usb_camera_rtsp.json
```

## 使用建议

- 先完成 RKNN runtime、Python 依赖和 FFmpeg 环境安装
- 再验证 `usb_camera_rtsp.json` 是否可启动
- 本地视频场景统一使用工程内目录：
  - `video/input`
  - `video/output`
- 如果要在板端提供 RTSP 服务，先确认 `mediamtx` 可启动
