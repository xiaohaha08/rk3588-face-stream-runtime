# RK3588 人脸识别视频系统

面向 RK3588 的单路实时视频推理工程，聚焦正式部署场景。

当前仓库提供完整的运行时主链路、控制面和部署资源，支持：

- USB 摄像头、RTSP 流、本地视频文件输入
- 人脸检测、质量分类、可选的人脸识别
- 处理结果叠框后推送到 RTSP，或保存为本地视频文件
- Web 控制面支持启动/停止运行时、实时预览、SSE/WebSocket 状态更新、Debug 页签、本地视频管理、人脸库和告警管理

本仓库已经去掉测试链路和阶段性验证资产，只保留正式运行所需代码与资源。

## 开源发布说明

- 本仓库按“自包含部署仓库”方式组织，`deploy_assets/` 和 `models/` 会随仓库一起发布，当前仓库体积约 `204 MB`，首次 clone 会相对较慢。
- 以下运行时目录和产物不纳入版本控制：`gallery/`、`video/input/`、`video/output/`、`outputs/face_alarm_images/`、`logs/`、`tmp/`、`temp/`，以及运行过程中生成的人脸库数据库、快照、日志和临时文件。
- 根目录 `LICENSE` 只覆盖本项目自有代码；随仓库分发的第三方二进制、wheel、运行时库和模型文件，仍分别受其上游许可证或分发条款约束。
- 第三方资源来源、许可证和发布前核验项见 `THIRD_PARTY_NOTICES.md`。如果其中某项的再分发权限还没有确认，请在公开发布前将对应文件移出仓库，并改为文档化下载步骤。

## 项目能力

- 单路实时视频推理：适配 RK3588 + RKNN Runtime 的正式运行链路
- 人脸检测与质量分类：在主链路完成检测和质量打分
- 人脸识别：基于人脸库进行身份匹配，空库时仍可运行检测和质量分类
- 结果叠加：在输出画面中绘制人脸框和识别标签
- 告警抓拍：目标身份出现时记录告警事件并周期性保存快照
- 控制面：提供基于 Flask 的浏览器控制台、实时预览、SSE、可选 WebSocket、配置切换与运行控制
- 人脸库管理：支持身份 CRUD、样本管理、运行时重载
- 本地视频管理：支持上传、选择、删除本地输入/输出视频，并生成浏览器预览
- 实时监控：查看 FPS、时延、丢帧、队列、CPU/NPU/内存/温度等状态，并提供阶段级性能调试视图，用于查看主链耗时、detector/classifier 耗时、识别 sidecar 耗时、队列深度、丢帧原因和最近链路快照
- 推流信息：当 MediaMTX 可用时，可直接查看 RTSP / HLS / WebRTC / RTMP 地址

## 目录说明

### 仓库内代码与资源

- `adapters/`：RKNN 模型适配层
- `api/`：Flask 控制面、SSE、WebSocket 接口
- `configs/`：运行配置样例
- `deploy_assets/`：离线部署资源、MediaMTX、FFmpeg、Python wheels
- `inputs/`：输入源实现
- `metrics/`：运行指标统计
- `models/`：默认模型文件
- `outputs/`：画面合成与输出实现
- `pipelines/`：主链路编排
- `scripts/`：启动入口
- `services/`：配置、运行管理、人脸库、告警、系统监控
- `stream/`：FFmpeg / MediaMTX 相关能力
- `web/`：控制台前端
- `workers/`：推理 worker

### 运行时目录

以下目录不是代码目录，首次使用时请自行创建：

- `gallery/`：人脸库目录，默认会在其中生成 `face_library.db` 和样本图片
- `video/input/`：本地视频输入目录
- `video/output/`：本地视频输出目录
- `outputs/face_alarm_images/`：告警快照默认目录

上述目录已在 `.gitignore` 中排除，建议仅作为本地运行数据目录使用。

## 运行环境

生产环境建议：

- 硬件：RK3588
- 系统：Linux `aarch64`
- Python：`3.9.x`
- 推理依赖：`rknn-toolkit-lite2`、`librknnrt.so`
- 媒体依赖：`ffmpeg`、`ffprobe`
- 控制面依赖：`flask`、`simple-websocket`（可选，SSE 不依赖）

仓库已提供离线部署资源：

- `deploy_assets/linux_arm64/rknn_runtime/`
- `deploy_assets/linux_arm64/python_wheels/`
- `deploy_assets/linux_arm64/mediamtx/`
- `deploy_assets/linux_arm64/ffmpeg/`

第三方资源的许可证边界和发布前核验项见 `THIRD_PARTY_NOTICES.md`。

## 快速开始

以下命令默认都在仓库根目录执行。

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

推荐直接使用仓库内的离线安装脚本：

```bash
chmod +x deploy_assets/linux_arm64/install_py39_offline.sh
./deploy_assets/linux_arm64/install_py39_offline.sh --with-api
```

如果你想手动安装，也可以执行：

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

### 3. 准备运行时目录

```bash
mkdir -p gallery video/input video/output outputs/face_alarm_images
```

### 4. 检查模型文件

默认配置依赖以下模型文件，仓库已经提供：

- `models/rk-yolov8n-face-to-rk3588-640x640.rknn`
- `models/ResNet_6face_model_210.onnx-to-rk3588-224x224.rknn`
- `models/XcFaceNet20241110-to-rk3588-160x160.rknn`

如需替换模型，请同步修改配置中的 `models.detector`、`models.classifier`、`models.recognizer`。

## 配置流程

建议从 `configs/` 中选一个最接近的样例开始修改。

### 第 1 步：选择场景模板

| 配置文件 | 输入类型 | 输出类型 | 适用场景 |
| --- | --- | --- | --- |
| `configs/usb_camera_rtsp.json` | USB 摄像头 | RTSP 推流 | 板端摄像头实时推流 |
| `configs/rtsp_input_rtsp.json` | RTSP 拉流 | RTSP 推流 | 拉一路 RTSP，处理后重新发布 |
| `configs/video_file_rtsp.json` | 本地视频文件 | RTSP 推流 | 对离线视频跑推理，并发布 RTSP |
| `configs/video_file_save.json` | 本地视频文件 | 本地文件保存 | 对离线视频跑推理，并保存处理结果 |

### 第 2 步：修改输入配置

- `input.kind`：可选 `usb_camera`、`rtsp`、`video_file`
- `input.device`：USB 摄像头设备号，如 `0`
- `input.path`：RTSP 地址或本地视频路径
- `input.ffmpeg_path` / `input.ffprobe_path`：默认填 `ffmpeg` / `ffprobe`，前提是已加入 `PATH`
- `input.io_backend`：`rtsp` / `video_file` 可选 `ffmpeg` 或 `opencv`

说明：

- 本地视频模式建议统一使用 `video/input/...`
- 配置中的相对路径都相对于仓库根目录解析

### 第 3 步：确认模型与识别能力

- `models.detector`：人脸检测模型，必填
- `models.classifier`：质量分类模型，建议保留
- `models.recognizer`：识别模型；如果为空，则不执行身份识别

说明：

- 即使启用了识别模型，如果 `gallery/` 中没有可用身份样本，系统仍会正常运行，但只做检测和质量分类，不输出已识别身份

### 第 4 步：配置人脸库

- `gallery.directory`：人脸库根目录，默认是 `gallery`
- `gallery.max_images_per_identity`：目录扫描模式下每个身份最多加载多少张图

控制台方式：

- 先启动控制面
- 在页面中的“人脸库管理”里新增身份、上传样本
- 样本会写入 `gallery/face_library_samples/`
- 数据库默认写入 `gallery/face_library.db`

### 第 5 步：配置输出方式

RTSP 推流模式：

- `output.sink` 设为 `ffmpeg_rtsp`
- `output.stream_url` 设为目标推流地址，例如 `rtsp://127.0.0.1:8554/live/usb-face-recog`
- `output.ffmpeg_path` 默认填 `ffmpeg`

本地保存模式：

- `output.sink` 设为 `ffmpeg_file`
- `output.output_path` 设为输出文件路径，例如 `video/output/demo_processed.mp4`

### 第 6 步：配置 MediaMTX

只有在 RTSP 推流模式下才需要重点关注这一段。

- `mediamtx.enabled`：是否启用 MediaMTX 服务探测/管理
- `mediamtx.auto_start`：端口未就绪时是否自动拉起仓库内的 `mediamtx`
- `mediamtx.binary_path`：默认是 `deploy_assets/linux_arm64/mediamtx/mediamtx`
- `mediamtx.config_path`：默认是 `deploy_assets/linux_arm64/mediamtx/mediamtx.yml`

说明：

- 如果 `mediamtx.enabled=true` 且 `auto_start=true`，运行时会优先尝试自动拉起服务
- 如果你已有外部 MediaMTX，只要目标端口可访问，运行时会直接复用

### 第 7 步：配置控制面与告警

- `api.host` / `api.port`：控制面监听地址
- `api.auto_start`：启动控制面时是否自动启动 Runtime
- `api.enable_websocket`：是否启用 `/ws`
- `alarm.enabled`：是否启用告警
- `alarm.image_dir`：告警快照目录，默认是 `outputs/face_alarm_images`

说明：

- 告警只针对已识别且启用的身份
- 未识别为目标身份的 `unknown` 不会进入告警库

## 启动方式

### 直接运行视频主链路

```bash
python3 scripts/run_video_runtime.py --config configs/usb_camera_rtsp.json --timeout 0
```

`--timeout 0` 表示持续运行，直到手动中断。

### 启动控制面

```bash
python3 scripts/run_control_plane.py --config configs/usb_camera_rtsp.json
```

默认情况下，如果配置里 `api.auto_start=true`，控制面启动后会自动拉起 Runtime。

如果只想先打开控制台、不自动启动：

```bash
python3 scripts/run_control_plane.py --config configs/video_file_save.json --no-autostart
```

浏览器访问：

- `http://设备IP:5000/`
- `http://设备IP:5000/dashboard`

## 控制台可以做什么

- 查看运行状态、模型状态、服务状态
- 启动 / 停止 Runtime
- 切换配置预设
- 查看实时 FPS、端到端时延、队列、丢帧和阶段耗时
- 在 Web 控制面的 Debug 页签查看主链阶段耗时、队列/丢帧、最近整帧链路快照和板端资源状态
- 查看 CPU / NPU / 内存 / 温度信息
- 查看推流地址和预览
- 管理本地 `video/input` / `video/output` 视频
- 管理人脸库身份和样本
- 查看、删除告警记录和快照

## 已验证的典型场景

### 1. USB 摄像头输入，处理后推送到 RTSP

```bash
python3 scripts/run_video_runtime.py --config configs/usb_camera_rtsp.json --timeout 0
```

### 2. RTSP 输入，处理后重新推送到 RTSP

```bash
python3 scripts/run_video_runtime.py --config configs/rtsp_input_rtsp.json --timeout 0
```

### 3. 本地视频输入，处理后推送到 RTSP

```bash
python3 scripts/run_video_runtime.py --config configs/video_file_rtsp.json --timeout 0
```

### 4. 本地视频输入，处理后保存到文件

```bash
python3 scripts/run_video_runtime.py --config configs/video_file_save.json --timeout 0
```

## 注意事项

- 当前仓库聚焦单路视频运行，不包含多路调度与训练部分
- 配置中的相对路径以仓库根目录为基准，尽量不要在配置里写本机绝对路径
- `ffmpeg` / `ffprobe` 不在 `PATH` 时，需要在配置中明确指定路径
- 本地视频模式下，请先确保输入文件已经放入 `video/input/`
- 浏览器不一定能直接播放所有原始视频格式，控制面会用 `ffmpeg` 自动生成兼容预览
- 如果识别结果没有命中，输出画面会显示为 `unknown`
- 如需关闭识别但保留检测链路，可在控制台关闭“人脸识别开关”，或移除 `models.recognizer`

## 更多部署资料

- [deploy_assets/README.md](deploy_assets/README.md)
- [deploy_assets/linux_arm64/INSTALL_PY39_OFFLINE.md](deploy_assets/linux_arm64/INSTALL_PY39_OFFLINE.md)
