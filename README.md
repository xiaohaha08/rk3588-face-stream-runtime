# RK3588 人脸识别视频工程

## 项目定位

这是一个面向正式运行的人脸识别视频工程，运行在 RK3588 平台，提供单一路径的实时处理链路：

- 输入源：`usb_camera`、`rtsp`、`video_file`
- 输出方式：`ffmpeg_rtsp`、`ffmpeg_file`
- 处理链路：人脸检测、质量分类、识别增强、告警快照、控制面展示

本仓库已经去掉测试链路和阶段性验证资产，只保留正式运行所需代码和资料。

## 目录说明

- `adapters/`：RKNN 模型适配层
- `api/`：Flask 控制面、SSE、WebSocket 接口
- `configs/`：正式运行配置
- `deploy_assets/`：离线部署与运行依赖
- `gallery/`：人脸库目录
- `inputs/`：正式输入源实现
- `metrics/`：运行指标统计
- `models/`：模型文件
- `outputs/`：画面合成与输出
- `pipelines/`：单路运行时主链路
- `scripts/`：正式启动入口
- `services/`：配置、装配、运行管理、告警、人脸库
- `stream/`：FFmpeg 和 MediaMTX 相关能力
- `video/input/`：本地输入视频目录
- `video/output/`：本地输出视频目录
- `web/`：前端控制台
- `workers/`：正式 worker 实现

## 正式配置

- `configs/usb_camera_rtsp.json`
  USB 摄像头输入，处理后推送到 RTSP。

- `configs/rtsp_input_rtsp.json`
  拉取 RTSP 输入流，处理后重新推送到 RTSP。

- `configs/video_file_rtsp.json`
  使用 `video/input/` 下的视频文件作为输入，处理后推送到 RTSP。

- `configs/video_file_save.json`
  使用 `video/input/` 下的视频文件作为输入，处理后保存到 `video/output/`。

## 正式命令

运行视频主链路：

```bash
python scripts/run_video_runtime.py --config configs/usb_camera_rtsp.json
```

运行控制面（主要）：

```bash
python3 /home/cat/xhh/workspace/scripts/run_control_plane.py
```

常见场景示例：

```bash
python scripts/run_video_runtime.py --config configs/rtsp_input_rtsp.json --timeout 0
python scripts/run_video_runtime.py --config configs/video_file_rtsp.json --timeout 0
python scripts/run_video_runtime.py --config configs/video_file_save.json --timeout 0
python scripts/run_control_plane.py --config configs/video_file_save.json --no-autostart
```

## 运行注意事项

- `video_file_*` 配置默认使用工程内相对路径：
  - 输入目录：`video/input`
  - 输出目录：`video/output`
- `mediamtx` 路径默认指向仓库内的 `deploy_assets/linux_arm64/mediamtx/`
- 控制面默认地址由配置中的 `api.host` 和 `api.port` 决定
- 浏览器访问：
  - `http://设备IP:5000/`
  - `http://设备IP:5000/dashboard`
- 如果浏览器不能直接播放本地输出视频，控制面会自动用 `ffmpeg` 生成预览文件

## 部署资料

- 部署资源说明：`deploy_assets/README.md`
- Linux ARM64 Python 3.9 离线安装：`deploy_assets/linux_arm64/INSTALL_PY39_OFFLINE.md`
