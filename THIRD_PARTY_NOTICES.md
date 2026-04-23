# Third-Party Notices

本仓库按“自包含部署仓库”方式打包了部分第三方资源，便于 RK3588 设备离线部署。

请注意：

- 根目录 `LICENSE` 只覆盖本项目自有代码，不自动覆盖仓库内 bundled 的第三方二进制、wheel、运行时库和模型文件。
- 本文件用于记录当前仓库中可识别的第三方资产来源、许可证和发布前核验项，不替代上游项目原始许可证文本。
- 只要某项资源的来源、许可证或再分发边界还没有确认，就不应假定它可以随仓库公开发布。

## 已能直接确认上游许可证的资产

| 路径/文件 | 可识别版本或名称 | 上游来源 | 许可证 | 再分发与署名要求 |
| --- | --- | --- | --- | --- |
| `deploy_assets/linux_arm64/mediamtx/mediamtx` | MediaMTX | `bluenviron/mediamtx` | MIT | 一般允许再分发；公开分发时应保留上游版权与许可证声明。 |
| `deploy_assets/linux_arm64/mediamtx/mediamtx_v1.9.0_linux_arm64v8.tar.gz` | MediaMTX v1.9.0 | `bluenviron/mediamtx` | MIT | 一般允许再分发；公开分发时应保留上游版权与许可证声明。 |
| `deploy_assets/windows_amd64/mediamtx/mediamtx.exe` | MediaMTX Windows 可执行文件 | `bluenviron/mediamtx` | MIT | 一般允许再分发；公开分发时应保留上游版权与许可证声明。 |
| `deploy_assets/linux_arm64/rknn_runtime/librknnrt.so` | Rockchip RKNN Runtime 共享库 | `airockchip/rknpu2` | BSD-3-Clause | 一般允许再分发；建议保留上游版权与许可证声明，并核对当前二进制的具体来源版本。 |
| `deploy_assets/linux_arm64/python_wheels/rknn_toolkit_lite2-2.3.0-cp39-cp39-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` | rknn-toolkit-lite2 2.3.0 | `airockchip/rknn-toolkit2` | BSD-3-Clause | 一般允许再分发；建议保留上游版权与许可证声明。当前 wheel 副本未内嵌清晰许可证文件时，发布前应再核对一次来源。 |

## 离线 Python Wheels

以下 wheel 位于 `deploy_assets/linux_arm64/python_wheels/py39_offline/`。这些包通常允许再分发，但仍应以 wheel 内自带的 `LICENSE` / `NOTICE` / `.dist-info` 元数据为准，并在公开发布时保留对应声明。

| 文件 | 包名/版本 | 许可证 | 备注 |
| --- | --- | --- | --- |
| `blinker-1.9.0-py3-none-any.whl` | blinker 1.9.0 | MIT | 允许再分发，保留上游许可证声明。 |
| `click-8.1.8-py3-none-any.whl` | click 8.1.8 | BSD | 允许再分发，保留上游许可证声明。 |
| `colorama-0.4.6-py2.py3-none-any.whl` | colorama 0.4.6 | BSD | 允许再分发，保留上游许可证声明。 |
| `flask-3.1.3-py3-none-any.whl` | Flask 3.1.3 | BSD-3-Clause | 允许再分发，保留上游许可证声明。 |
| `h11-0.16.0-py3-none-any.whl` | h11 0.16.0 | MIT | 允许再分发，保留上游许可证声明。 |
| `importlib_metadata-9.0.0-py3-none-any.whl` | importlib-metadata 9.0.0 | Apache-2.0 | 允许再分发，保留上游许可证与 NOTICE。 |
| `itsdangerous-2.2.0-py3-none-any.whl` | itsdangerous 2.2.0 | BSD-3-Clause | 允许再分发，保留上游许可证声明。 |
| `jinja2-3.1.6-py3-none-any.whl` | Jinja2 3.1.6 | BSD-3-Clause | 允许再分发，保留上游许可证声明。 |
| `markupsafe-3.0.3-cp39-cp39-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl` | MarkupSafe 3.0.3 | BSD-3-Clause | 允许再分发，保留上游许可证声明。 |
| `numpy-2.0.2-cp39-cp39-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` | NumPy 2.0.2 | BSD | 允许再分发，但 wheel 内还包含额外第三方 notices，应一并保留。 |
| `opencv_python_headless-4.13.0.92-cp37-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl` | opencv-python-headless 4.13.0.92 | Apache-2.0 | 允许再分发；除主许可证外，还应保留 wheel 内的第三方许可证说明。 |
| `psutil-7.2.2-cp36-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl` | psutil 7.2.2 | BSD-3-Clause | 允许再分发，保留上游许可证声明。 |
| `ruamel_yaml-0.19.1-py3-none-any.whl` | ruamel.yaml 0.19.1 | MIT | 允许再分发，保留上游许可证声明。 |
| `simple_websocket-1.1.0-py3-none-any.whl` | simple-websocket 1.1.0 | MIT | 允许再分发，保留上游许可证声明。 |
| `werkzeug-3.1.8-py3-none-any.whl` | Werkzeug 3.1.8 | BSD-3-Clause | 允许再分发；wheel 内还包含图标资源许可证说明，应一并保留。 |
| `wsproto-1.2.0-py3-none-any.whl` | wsproto 1.2.0 | MIT | 允许再分发，保留上游许可证声明。 |
| `zipp-3.23.0-py3-none-any.whl` | zipp 3.23.0 | MIT | 允许再分发，保留上游许可证声明。 |

## 公开发布前必须二次确认的资产

| 路径/文件 | 可识别版本或名称 | 当前判断 | 发布前动作 |
| --- | --- | --- | --- |
| `deploy_assets/linux_arm64/ffmpeg/ffmpeg-rockchip-usr.tar.gz` | Rockchip 目标环境 FFmpeg 资源包 | 仓库中仅能确认其包含 `ffmpeg` / `ffprobe` / `libav*` / `librockchip_mpp.so` / `librga.so` 等二进制，但无法从当前仓库直接确认精确源码版本、构建参数以及是否启用了 GPL 组件。FFmpeg 官方说明默认遵循 LGPL-2.1-or-later，但启用 GPL 组件后整包将转为 GPL。 | 公开发布前补充构建来源、版本、构建脚本和随包许可证；如果做不到，建议将该压缩包移出仓库，改为文档化安装步骤。 |
| `models/rk-yolov8n-face-to-rk3588-640x640.rknn` | RKNN 检测模型 | 当前仓库未记录原始模型来源、转换过程和上游许可证。 | 公开发布前补充来源、版本、转换脚本和许可证；如果做不到，建议移出仓库并改为手动下载/转换。 |
| `models/ResNet_6face_model_210.onnx-to-rk3588-224x224.rknn` | RKNN 质量分类模型 | 当前仓库未记录原始模型来源、转换过程和上游许可证。 | 公开发布前补充来源、版本、转换脚本和许可证；如果做不到，建议移出仓库并改为手动下载/转换。 |
| `models/XcFaceNet20241110-to-rk3588-160x160.rknn` | RKNN 识别模型 | 当前仓库未记录原始模型来源、转换过程和上游许可证。 | 公开发布前补充来源、版本、转换脚本和许可证；如果做不到，建议移出仓库并改为手动下载/转换。 |
| `models/yolov8_i8.rknn` | 额外 RKNN 模型文件 | 当前仓库未记录该文件用途、原始模型来源和许可证。 | 若并非发布必需，建议直接移出仓库；若需要保留，则先补齐来源、用途和许可证。 |

## 官方参考链接

- MediaMTX: <https://github.com/bluenviron/mediamtx>
- FFmpeg licensing: <https://ffmpeg.org/legal.html>
- RKNN Toolkit2: <https://github.com/airockchip/rknn-toolkit2>
- RKNPU2 / RKNN Runtime: <https://github.com/airockchip/rknpu2>

## 维护建议

- 公开发布前，优先处理“公开发布前必须二次确认的资产”这一节。
- 如果某项资源无法确认再分发权限，就不要继续随仓库公开分发；改为在 `README.md` 中写清下载方式、版本和校验信息。
- 对于已经确认可分发的 wheel/binary，建议在后续版本中补齐对应的 LICENSE/NOTICE 副本，避免只依赖上游链接。

本文件仅用于仓库维护和开源发布准备，不构成法律意见。
