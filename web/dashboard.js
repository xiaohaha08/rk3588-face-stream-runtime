(() => {
  const MODE_META = {
    face_recognition: {
      label: "人脸识别",
      description: "人脸检测 + 质量分类 + 识别旁路",
    },
  };

  const BROWSER_NATIVE_VIDEO_SUFFIXES = new Set([".mp4", ".webm", ".m4v", ".mov"]);
  const RUNTIME_STATE_LABELS = {
    stopped: "已停止",
    starting: "启动中",
    running: "运行中",
    stopping: "停止中",
    error: "错误",
  };

  const LOW_FREQUENCY_REFRESH_TOPICS = new Set([
    "runtime.state",
    "runtime.mode",
    "runtime.error",
    "models.state",
    "stream.mediamtx",
    "input.info",
  ]);

  const DEBUG_STAGE_META = {
    capture_read: {label: "采集读取", note: "source.read / 解码"},
    capture_to_worker_start: {label: "进入 Worker 前等待", note: "latest-wins / worker 空闲等待"},
    worker_process: {label: "平均推理时间", note: "worker_process / worker 数"},
    detector: {label: "Detector", note: "检测模型推理与后处理"},
    classifier: {label: "Classifier", note: "质量分类累计耗时"},
    output_wait: {label: "输出等待", note: "worker 结果到 output 线程的等待"},
    output_prepare: {label: "输出准备", note: "识别融合 + 叠框 + 报警"},
    enrich: {label: "识别融合", note: "track / sidecar 结果回填"},
    compose: {label: "画框合成", note: "FrameComposer"},
    alarm: {label: "报警处理", note: "快照 / 落盘 / 数据库"},
    push_queue_wait: {label: "推流队列等待", note: "packet 在 push_queue 中等待"},
    push_write: {label: "推流写入", note: "sink.write / FFmpeg stdin"},
    recognition_extract: {label: "Sidecar 提特征", note: "embedding 提取"},
    recognition_match: {label: "Sidecar 匹配", note: "gallery match"},
    recognition_total: {label: "Sidecar 总耗时", note: "识别线程总耗时"},
    e2e_total: {label: "端到端", note: "capture 到推流写入完成"},
  };

  const DEBUG_MAIN_STAGE_KEYS = [
    "capture_read",
    "capture_to_worker_start",
    "worker_process",
    "output_wait",
    "output_prepare",
    "push_queue_wait",
    "push_write",
  ];
  const DEBUG_WORKER_STAGE_KEYS = ["detector", "classifier"];
  const DEBUG_OUTPUT_STAGE_KEYS = ["enrich", "compose", "alarm"];
  const DEBUG_SIDECAR_STAGE_KEYS = ["recognition_extract", "recognition_match", "recognition_total"];

  const state = {
    pollingTimer: null,
    eventSource: null,
    websocket: null,
    bundle: null,
    configPresets: [],
    currentPreviewUrl: "",
    currentPreviewMode: "",
    alarmEnabled: false,
    faceRecognitionEnabled: false,
    localVideos: null,
    selectedLocalInput: "",
    localPlayerPath: "",
    faceLibraryIdentities: [],
    faceLibrarySamples: [],
    faceLibraryPaths: null,
    faceLibrarySummary: null,
    selectedIdentityId: 0,
    dialogMode: "",
    pendingConfigPath: "",
    configSelectionDirty: false,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
  const norm = (value) => String(value || "").trim().replace(/\\/g, "/").toLowerCase();
  const num = (value, digits = 1) => (Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--");
  const ts = (value) => {
    const raw = Number(value);
    if (!Number.isFinite(raw) || raw <= 0) return "--";
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString();
  };
  const tsIso = (value) => {
    const date = new Date(String(value || ""));
    return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString();
  };
  const formatFileSize = (value) => {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 KB";
    if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  };

  function joinUrlPath(base, relativePath) {
    const prefix = String(base || "").replace(/\/+$/, "");
    const raw = String(relativePath || "").trim().replace(/^\/+/, "");
    if (!prefix || !raw) return "";
    return `${prefix}/${raw.split("/").map((part) => encodeURIComponent(part)).join("/")}`;
  }

  function withCacheBust(url, value) {
    const raw = String(url || "").trim();
    if (!raw || raw === "#") return "";
    const stamp = encodeURIComponent(String(value || Date.now()));
    return `${raw}${raw.includes("?") ? "&" : "?"}ts=${stamp}`;
  }

  function resolveIdentityImageUrl(item) {
    const direct = String(item?.image_url || "").trim();
    if (direct) return direct;
    const sampleId = Number(item?.latest_sample_id || 0);
    return sampleId > 0 ? `/api/face-library/samples/${sampleId}/image` : "";
  }

  function resolveSampleImageUrl(item) {
    const direct = String(item?.image_url || "").trim();
    if (direct) return direct;
    const sampleId = Number(item?.id || 0);
    return sampleId > 0 ? `/api/face-library/samples/${sampleId}/image` : "";
  }

  function resolveAlarmImageUrl(snapshot) {
    const direct = String(snapshot?.image_url || "").trim();
    if (direct) return direct;
    return joinUrlPath("/api/alarms/images", snapshot?.image_path || snapshot?.image_rel_path || "");
  }

  function setText(id, value) {
    const element = $(id);
    if (element) element.textContent = value;
  }

  function setValue(id, value) {
    const element = $(id);
    if (element) element.value = value;
  }

  function setHtml(id, value) {
    const element = $(id);
    if (element) element.innerHTML = value;
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function setRealtimeMode(text) {
    setText("realtime-mode", text);
  }

  function formatRuntimeState(value) {
    const key = String(value || "stopped").trim().toLowerCase();
    return RUNTIME_STATE_LABELS[key] || value || "未知";
  }

  function setRuntimeBadge(text) {
    const dot = $("runtime-dot");
    const label = $("runtime-text");
    if (!dot || !label) return;
    const value = String(text || "stopped");
    dot.classList.toggle("online", value === "running" || value === "starting");
    label.textContent = formatRuntimeState(value);
  }

  function rememberPendingConfigPath(path) {
    state.pendingConfigPath = String(path || "").trim();
    state.configSelectionDirty = true;
  }

  function syncConfigSelection(runtimeConfigPath) {
    const runtimePath = String(runtimeConfigPath || "").trim();
    if (!state.configSelectionDirty || norm(state.pendingConfigPath) === norm(runtimePath)) {
      state.pendingConfigPath = runtimePath;
      state.configSelectionDirty = false;
    }
    const visiblePath = state.configSelectionDirty ? state.pendingConfigPath : runtimePath;
    renderPresetOptions(visiblePath);
    setValue("config-path", visiblePath);
    return visiblePath;
  }

  function localizeStaticText() {
    document.title = "RK3588 视频推理控制台";
    setText("btn-start", "启动");
    setText("btn-stop", "停止");
    setText("btn-refresh", "刷新状态");
    setText("tab-preview", "实时预览");
    setText("tab-debug", "Debug");
    setText("tab-camera", "\u6444\u50cf\u5934\u4fe1\u606f");
    setText("tab-local-videos", "本地视频");
    setText("tab-streams", "流地址");
    setText("tab-alarms", "报警信息");
    setText("tab-inspector", "人脸库管理");
    setText("face-lib-create", "新增");
    setText("face-lib-edit", "修改");
    setText("face-lib-refresh", "刷新");
    setText("face-lib-delete-all", "全部删除");
    setText("face-lib-guide", "使用说明");
    setText("face-lib-upload", "添加样本");
    setText("face-lib-reload-runtime", "重载识别运行时");
    setText("face-lib-guide-close", "关闭");
    setText("face-lib-dialog-cancel", "取消");
    setText("face-lib-preview-close", "关闭");
    setText("service-sse", "可用");
    setText("preview-overlay", "等待视频内容");
    setText("local-player-title", "未选择视频");
    setText("stream-summary", "等待流地址");
    setText("realtime-mode", "等待连接");
    if ($("config-path")) $("config-path").placeholder = "运行配置文件路径";
    if ($("face-lib-search")) $("face-lib-search").placeholder = "输入姓名或备注关键词";
    if ($("face-lib-dialog-name")) $("face-lib-dialog-name").placeholder = "请输入姓名";
    if ($("face-lib-dialog-note")) $("face-lib-dialog-note").placeholder = "请输入备注";
  }

  function findPresetByPath(path) {
    return state.configPresets.find((item) => norm(item.path) === norm(path)) || null;
  }

  function ensurePreset(path) {
    if (!path || findPresetByPath(path)) return;
    state.configPresets = [{
      category: "当前配置",
      label: "手动路径",
      description: "当前配置不在预设列表中。",
      path,
      filename: "",
    }, ...state.configPresets];
  }

  function isLocalVideoPreset(preset) {
    return Boolean(preset && ["video_file_rtsp.json", "video_file_save.json"].includes(String(preset.filename || "")));
  }

  function isLocalVideoConfig(config) {
    return String(config?.input?.kind || "").trim().toLowerCase() === "video_file";
  }

  function renderPresetSummary(preset) {
    setText("preset-category", preset?.category || "模式预设");
    setText("preset-title", preset?.label || "等待选择");
    setText("preset-description", preset?.description || "请选择要运行的模式。");
    const panel = $("local-start-panel");
    if (panel) panel.classList.toggle("hidden", !isLocalVideoPreset(preset));
  }

  function renderPresetOptions(path) {
    const select = $("config-preset");
    if (!select) return;
    ensurePreset(path);
    const current = norm(path);
    select.innerHTML = state.configPresets.map((item) => (
      `<option value="${esc(item.path)}"${norm(item.path) === current ? " selected" : ""}>${esc(item.category)} / ${esc(item.label)}</option>`
    )).join("");
    renderPresetSummary(findPresetByPath(path) || state.configPresets[0] || null);
  }

  function renderAlarmToggle(config) {
    const enabled = Boolean((config?.alarm || {}).enabled);
    state.alarmEnabled = enabled;
    setText("alarm-toggle-status", enabled ? "已开启" : "已关闭");
    const button = $("btn-alarm-toggle");
    if (!button) return;
    button.textContent = enabled ? "关闭报警" : "开启报警";
    button.classList.toggle("btn-danger", enabled);
    button.classList.toggle("btn-secondary", !enabled);
  }

  function renderFaceRecognitionToggle(config) {
    const enabled = Boolean(String(config?.models?.recognizer || "").trim());
    state.faceRecognitionEnabled = enabled;
    setText("face-recognition-toggle-status", enabled ? "已开启" : "已关闭");
    const button = $("btn-face-recognition-toggle");
    if (!button) return;
    button.textContent = enabled ? "关闭识别" : "开启识别";
    button.classList.toggle("btn-danger", enabled);
    button.classList.toggle("btn-secondary", !enabled);
  }

  function renderSystem(system) {
    const cpu = system?.cpu || {};
    const memory = system?.memory || {};
    const npu = system?.npu || {};
    setText("system-cpu-usage", Number.isFinite(Number(cpu.percent)) ? `${Number(cpu.percent).toFixed(1)}%` : "--");
    setText("system-memory-usage", Number.isFinite(Number(memory.percent)) ? `${Number(memory.percent).toFixed(1)}%` : "--");
    setText("system-cpu-temp", Number.isFinite(Number(cpu.temperature_c)) ? `${Number(cpu.temperature_c).toFixed(1)}°C` : "--");
    setText("system-npu-temp", Number.isFinite(Number(npu.temperature_c)) ? `${Number(npu.temperature_c).toFixed(1)}°C` : "--");
    setText("system-memory-amount", Number.isFinite(Number(memory.used_mb)) && Number.isFinite(Number(memory.total_mb)) && Number(memory.total_mb) > 0 ? `${Number(memory.used_mb).toFixed(0)} / ${Number(memory.total_mb).toFixed(0)} MB` : "--");
  }

  function getSourceMetrics(runtimeMetrics, sourceId) {
    return ((runtimeMetrics || {}).sources || {})[sourceId] || {};
  }

  function renderMetrics(runtimeMetrics, sourceId) {
    const metrics = getSourceMetrics(runtimeMetrics, sourceId);
    setText("metric-capture-fps", num(metrics.capture_fps, 1));
    setText("metric-dispatch-fps", num(metrics.dispatch_fps, 1));
    setText("metric-output-fps", num(metrics.output_fps, 1));
    setText("metric-worker-ms", `${num(metrics.worker_avg_ms, 1)} ms`);
    setText("metric-e2e-ms", `${num(metrics.e2e_avg_ms, 1)} ms`);
    setText("metric-drop-rate", `${num(Number(metrics.dropped_rate || 0) * 100, 2)}%`);
    setText("metric-rec-submit", String(metrics.recognition_submit_count || 0));
    setText("metric-rec-match", String(metrics.recognition_match_count || 0));
  }

  function formatFps(value, digits = 1) {
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric > 0 ? `${numeric.toFixed(digits)} 帧/秒` : "--";
  }

  function formatResolution(width, height) {
    const w = Number(width);
    const h = Number(height);
    return Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0 ? `${Math.round(w)} × ${Math.round(h)} 像素` : "--";
  }

  function formatBitrate(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) return "--";
    return numeric >= 1000 ? `${(numeric / 1000).toFixed(2)} Mb/s` : `${Math.round(numeric)} kb/s`;
  }

  function formatInputKind(value) {
    const key = String(value || "").trim().toLowerCase();
    const labels = {
      usb_camera: "USB 摄像头",
      rtsp: "RTSP 拉流",
      video_file: "本地视频文件",
    };
    return labels[key] || value || "--";
  }

  function formatOpened(value) {
    return value ? "已打开" : "未打开";
  }

  function renderInfoCards(containerId, items) {
    const container = $(containerId);
    if (!container) return;
    container.innerHTML = items.map((item) => `
      <div class="camera-info-card">
        <span>${esc(item.label)}</span>
        <strong>${esc(item.value)}</strong>
        ${item.note ? `<small>${esc(item.note)}</small>` : ""}
      </div>
    `).join("");
  }

  function renderCameraInfo(runtime, config, source) {
    const sourceId = runtime.source_id || source.source_id || "";
    const metrics = getSourceMetrics(runtime.metrics || {}, sourceId);
    const inputInfo = runtime.input_info || source.input_info || {};
    const outputInfo = runtime.output_info || source.output_info || {};
    const inputConfig = config.input || {};
    const outputConfig = config.output || {};
    const observedFps = Number(inputInfo.observed_fps || metrics.capture_fps || 0);
    const reportedFps = Number(inputInfo.fps || inputConfig.fps || 0);
    const effectiveFps = Number(inputInfo.effective_fps || 0) || (observedFps > 0 ? observedFps : reportedFps);
    const targetOutputFps = Number(outputInfo.target_fps || 0) || (effectiveFps > 0 ? Math.min(30, effectiveFps) : Number(outputConfig.fps || 0));
    const target = String(inputInfo.target || inputConfig.path || inputConfig.device || "").trim() || "--";

    renderInfoCards("camera-info-grid", [
      {label: "输入类型", value: formatInputKind(inputInfo.kind || inputConfig.kind), note: inputInfo.io_backend ? `采集后端：${inputInfo.io_backend}` : ""},
      {label: "输入目标", value: target, note: `状态：${formatOpened(inputInfo.opened)}`},
      {label: "分辨率", value: formatResolution(inputInfo.width || inputConfig.width, inputInfo.height || inputConfig.height), note: "优先显示实际帧尺寸"},
      {label: "上报帧率", value: formatFps(reportedFps), note: "来自摄像头驱动 / ffprobe / 配置"},
      {label: "实测帧率", value: formatFps(observedFps), note: "运行时采集窗口统计"},
      {label: "输入码率", value: formatBitrate(inputInfo.bitrate_kbps), note: "驱动或 ffprobe 支持时显示"},
      {label: "编码格式", value: inputInfo.codec || "--", note: inputInfo.pixel_format ? `像素格式：${inputInfo.pixel_format}` : ""},
      {label: "时长 / 帧数", value: Number(inputInfo.duration_seconds || 0) > 0 ? `${Number(inputInfo.duration_seconds).toFixed(1)} 秒` : "--", note: inputInfo.frame_count ? `${inputInfo.frame_count} 帧` : ""},
    ]);

    renderInfoCards("camera-policy-grid", [
      {label: "有效输入帧率", value: formatFps(effectiveFps), note: "实测帧率可修正不准确的上报帧率"},
      {label: "目标输出帧率", value: formatFps(targetOutputFps), note: "取有效输入帧率、30 帧/秒、较低 output.fps 的最小值"},
      {label: "输出上限", value: formatFps(outputInfo.fps_cap || 30, 0), note: "板端输出最高帧率"},
      {label: "实际输出帧率", value: formatFps(metrics.output_fps), note: "运行时推流窗口统计"},
      {label: "限帧丢弃", value: String((metrics.drop_reasons || {}).output_fps_cap || 0), note: "因输出限帧丢弃的旧帧数量"},
      {label: "输出编码", value: outputConfig.video_encoder || "--", note: outputConfig.video_bitrate ? `目标码率：${outputConfig.video_bitrate}` : ""},
    ]);

    setText("camera-info-json", JSON.stringify({
      "输入信息": inputInfo,
      "输出策略": outputInfo,
      "采集帧率": metrics.capture_fps || 0,
      "输出帧率": metrics.output_fps || 0,
      "丢帧原因": metrics.drop_reasons || {},
    }, null, 2));
  }

  function formatMs(value, digits = 1) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${numeric.toFixed(digits)} ms` : "--";
  }

  function stageMeta(key) {
    return DEBUG_STAGE_META[key] || {label: key, note: ""};
  }

  function stageValue(timings, key) {
    const numeric = Number(timings?.[key]);
    return Number.isFinite(numeric) ? numeric : 0;
  }

  function workerCountFromConfig(config) {
    const count = Number(config?.workers?.count);
    return Number.isFinite(count) && count > 0 ? count : 1;
  }

  function timingForDisplay(timings, workerCount) {
    const next = {...(timings || {})};
    const workerMs = Number(next.worker_process);
    if (Number.isFinite(workerMs)) {
      next.worker_process = workerMs / Math.max(1, workerCount);
    }
    return next;
  }

  function pickDisplayBottleneck(timingAvg) {
    return DEBUG_MAIN_STAGE_KEYS
      .map((key) => [key, stageValue(timingAvg, key)])
      .filter(([, value]) => value > 0)
      .sort((left, right) => right[1] - left[1])[0] || ["", 0];
  }

  function renderStageRows(containerId, keys, timingAvg, timingLast, referenceMs, bottleneckStage) {
    const container = $(containerId);
    if (!container) return;
    const maxStageMs = Math.max(0.1, ...keys.map((key) => stageValue(timingAvg, key)));
    container.innerHTML = keys.map((key) => {
      const meta = stageMeta(key);
      const avgMs = stageValue(timingAvg, key);
      const lastMs = stageValue(timingLast, key);
      const share = referenceMs > 0 ? (avgMs / referenceMs) * 100 : 0;
      const width = avgMs > 0 ? Math.max(2, (avgMs / maxStageMs) * 100) : 0;
      const isBottleneck = key === bottleneckStage;
      return `
        <div class="debug-stage-row${isBottleneck ? " is-bottleneck" : ""}">
          <div class="debug-stage-head">
            <strong>${esc(meta.label)}</strong>
            <span>${esc(meta.note || "")}</span>
          </div>
          <div class="debug-stage-values">
            <strong>avg ${esc(formatMs(avgMs))}</strong>
            <span>last ${esc(formatMs(lastMs))}</span>
            <span>${esc(share.toFixed(1))}% of E2E</span>
          </div>
          <div class="debug-stage-bar"><span style="width:${Math.max(0, Math.min(100, width)).toFixed(1)}%"></span></div>
        </div>
      `;
    }).join("");
  }

  function renderDebug(runtimeMetrics, sourceId, system, config) {
    const metrics = getSourceMetrics(runtimeMetrics, sourceId);
    const workerCount = workerCountFromConfig(config);
    const timingAvg = timingForDisplay(metrics.timing_avg_ms || {}, workerCount);
    const timingLast = timingForDisplay(metrics.timing_last_ms || {}, workerCount);
    const lastPipeline = timingForDisplay(metrics.last_pipeline_timing_ms || {}, workerCount);
    const e2eAvgMs = Number(metrics.e2e_avg_ms || timingAvg.e2e_total || 0);
    const [displayBottleneckStage, displayBottleneckAvgMs] = pickDisplayBottleneck(timingAvg);
    const bottleneckStage = displayBottleneckStage || String(metrics.bottleneck_stage || "").trim();
    const bottleneckMeta = stageMeta(bottleneckStage || "worker_process");
    const queueDepths = metrics.queue_depths || {};
    const queueHighWatermarks = metrics.queue_high_watermarks || {};
    const queueNames = Array.from(new Set([
      ...Object.keys(queueDepths),
      ...Object.keys(queueHighWatermarks),
    ])).sort();
    const summary = $("debug-summary-grid");
    if (summary) {
      summary.innerHTML = [
        {
          label: "当前瓶颈",
          value: bottleneckMeta.label || "--",
          extra: bottleneckStage ? `${formatMs(displayBottleneckAvgMs || metrics.bottleneck_avg_ms)} avg` : "暂无有效阶段耗时",
          bottleneck: true,
        },
        {
          label: "输出帧率",
          value: `${num(metrics.output_fps, 1)} fps`,
          extra: `capture ${num(metrics.capture_fps, 1)} / dispatch ${num(metrics.dispatch_fps, 1)}`,
        },
        {
          label: "端到端均值",
          value: formatMs(e2eAvgMs),
          extra: `worker ${formatMs(stageValue(timingAvg, "worker_process"))} / ${workerCount} 线程`,
        },
        {
          label: "丢帧情况",
          value: String(metrics.dropped_count || 0),
          extra: `drop rate ${num(Number(metrics.dropped_rate || 0) * 100, 2)}%`,
        },
        {
          label: "板端负载",
          value: `${num(system?.cpu?.percent, 1)}% CPU / ${num(system?.npu?.percent, 1)}% NPU`,
          extra: `CPU ${num(system?.cpu?.temperature_c, 1)}°C / NPU ${num(system?.npu?.temperature_c, 1)}°C`,
        },
      ].map((item) => `
        <div class="debug-summary-card${item.bottleneck ? " is-bottleneck" : ""}">
          <span>${esc(item.label)}</span>
          <strong>${esc(item.value)}</strong>
          <small>${esc(item.extra)}</small>
        </div>
      `).join("");
    }

    renderStageRows("debug-main-stage-list", DEBUG_MAIN_STAGE_KEYS, timingAvg, timingLast, e2eAvgMs, bottleneckStage);
    renderStageRows("debug-worker-stage-list", DEBUG_WORKER_STAGE_KEYS, timingAvg, timingLast, e2eAvgMs, "");
    renderStageRows("debug-output-stage-list", DEBUG_OUTPUT_STAGE_KEYS, timingAvg, timingLast, e2eAvgMs, "");
    renderStageRows("debug-sidecar-stage-list", DEBUG_SIDECAR_STAGE_KEYS, timingAvg, timingLast, e2eAvgMs, "");

    const queueGrid = $("debug-queue-grid");
    if (queueGrid) {
      if (!queueNames.length) {
        queueGrid.innerHTML = '<div class="file-empty">waiting for queue metrics</div>';
      } else {
        queueGrid.innerHTML = queueNames.map((name) => `
          <div class="debug-queue-item">
            <span>${esc(name)}</span>
            <strong>current ${esc(String(queueDepths[name] ?? 0))}</strong>
            <small>high watermark ${esc(String(queueHighWatermarks[name] ?? 0))}</small>
          </div>
        `).join("");
      }
    }

    setText("debug-drop-panel", JSON.stringify({
      dropped_count: metrics.dropped_count || 0,
      dropped_rate: Number(metrics.dropped_rate || 0),
      drop_reasons: metrics.drop_reasons || {},
      reorder_wait_count: metrics.reorder_wait_count || 0,
      reorder_skip_count: metrics.reorder_skip_count || 0,
      worker_timeout_count: metrics.worker_timeout_count || 0,
      recent_exception_count: metrics.recent_exception_count || 0,
      recognition_submit_count: metrics.recognition_submit_count || 0,
      recognition_result_count: metrics.recognition_result_count || 0,
      recognition_match_count: metrics.recognition_match_count || 0,
      recognition_reject_count: metrics.recognition_reject_count || 0,
    }, null, 2));
    setText("debug-last-pipeline", JSON.stringify(lastPipeline, null, 2));
    setText("debug-system-panel", JSON.stringify({
      cpu: system?.cpu || {},
      memory: system?.memory || {},
      npu: system?.npu || {},
      thermal: system?.thermal || {},
    }, null, 2));
  }

  function activatePreview(mode) {
    const frame = $("stream-frame");
    const video = $("stream-video");
    if (!frame || !video) return;
    frame.classList.toggle("active", mode === "iframe");
    video.classList.toggle("active", mode === "video");
    state.currentPreviewMode = mode;
  }

  function renderPreview(streams) {
    const frame = $("stream-frame");
    const video = $("stream-video");
    const overlay = $("preview-overlay");
    const hint = $("preview-hint");
    if (!frame || !video || !overlay || !hint) return;

    const webrtcUrl = String(streams.webrtc_url || "");
    const hlsUrl = String(streams.hls_url || "");
    const rtspUrl = String(streams.rtsp_url || "");

    if (webrtcUrl) {
      if (state.currentPreviewUrl !== webrtcUrl || state.currentPreviewMode !== "iframe") {
        frame.src = webrtcUrl;
        video.removeAttribute("src");
        video.load();
        state.currentPreviewUrl = webrtcUrl;
      }
      activatePreview("iframe");
      overlay.style.display = "none";
      hint.textContent = "";
      return;
    }

    if (hlsUrl) {
      if (state.currentPreviewUrl !== hlsUrl || state.currentPreviewMode !== "video") {
        frame.removeAttribute("src");
        video.src = hlsUrl;
        video.load();
        state.currentPreviewUrl = hlsUrl;
      }
      activatePreview("video");
      overlay.style.display = "none";
      hint.textContent = "";
      return;
    }

    frame.removeAttribute("src");
    video.removeAttribute("src");
    video.load();
    activatePreview("");
    state.currentPreviewUrl = "";
    overlay.style.display = "flex";
    overlay.textContent = rtspUrl ? "浏览器暂时无法直接预览，请使用下方 RTSP/HLS/WebRTC 地址。" : "等待流地址";
    hint.textContent = rtspUrl ? rtspUrl : "";
  }

  function renderStreamSummary(streams) {
    [["rtsp-link", streams.rtsp_url, "RTSP 暂不可用"], ["hls-link", streams.hls_url, "HLS 暂不可用"], ["webrtc-link", streams.webrtc_url, "WebRTC 暂不可用"], ["rtmp-link", streams.rtmp_url, "RTMP 暂不可用"]].forEach(([id, url, fallback]) => {
      const element = $(id);
      if (!element) return;
      element.href = url || "#";
      element.textContent = url || fallback;
    });
    setText("stream-summary", JSON.stringify({
      path: streams.path || "",
      publish_url: streams.publish_url || "",
      rtsp_url: streams.rtsp_url || "",
      hls_url: streams.hls_url || "",
      webrtc_url: streams.webrtc_url || "",
      rtmp_url: streams.rtmp_url || "",
    }, null, 2));
  }

  function ensureLocalInputSelection(localVideos) {
    const files = Array.isArray(localVideos?.input_files) ? localVideos.input_files : [];
    const current = String(state.selectedLocalInput || "").trim();
    if (current && files.some((item) => norm(item.path) === norm(current))) return;
    const selected = String(localVideos?.selected_input || "").trim();
    if (selected && files.some((item) => norm(item.path) === norm(selected))) {
      state.selectedLocalInput = selected;
      return;
    }
    state.selectedLocalInput = files[0]?.path || "";
  }

  function isBrowserFriendlyLocalVideo(file) {
    const name = String(file?.name || "").toLowerCase();
    const suffix = name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";
    const mimeType = String(file?.mime_type || "").toLowerCase();
    return BROWSER_NATIVE_VIDEO_SUFFIXES.has(suffix) || mimeType === "video/mp4" || mimeType === "video/webm";
  }

  function setLocalPlayer(file, force = false) {
    if (!file) return;
    const path = String(file.path || "");
    const player = $("local-video-player");
    if (!player) return;
    if (!force && norm(state.localPlayerPath) === norm(path)) return;
    state.localPlayerPath = path;
    const directUrl = String(file.url || "");
    const previewUrl = String(file.preview_url || "");
    const primaryUrl = isBrowserFriendlyLocalVideo(file) ? (directUrl || previewUrl) : (previewUrl || directUrl);
    const fallbackUrl = primaryUrl === directUrl ? previewUrl : directUrl;
    player.dataset.fallbackUrl = fallbackUrl;
    player.src = primaryUrl;
    player.load();
    setText("local-player-title", `${file.folder} / ${file.name}`);
  }

  function clearLocalPlayer() {
    const player = $("local-video-player");
    state.localPlayerPath = "";
    if (player) {
      player.removeAttribute("src");
      player.dataset.fallbackUrl = "";
      player.load();
    }
    setText("local-player-title", "未选择视频");
  }

  function renderVideoFileList(files, folder, selectedPath, allowInputSelection) {
    if (!files.length) return `<div class="file-empty">${esc(folder)} 文件夹为空。</div>`;
    return files.map((file) => {
      const active = norm(file.path) === norm(selectedPath);
      return `
        <article class="video-file-card${active ? " active" : ""}" data-folder="${esc(folder)}" data-path="${esc(file.path)}">
          <div class="video-file-head">
            <strong>${esc(file.name)}</strong>
            <span>${esc(ts(file.modified_at_ms))}</span>
          </div>
          <div class="video-file-meta">${esc(formatFileSize(file.size_bytes))}</div>
          <div class="video-file-actions">
            ${allowInputSelection ? '<button class="btn btn-secondary mini-btn" data-action="use-input" type="button">设为输入</button>' : ""}
            <button class="btn btn-secondary mini-btn" data-action="preview" type="button">预览</button>
            <button class="btn btn-danger mini-btn" data-action="delete-video" type="button">删除</button>
          </div>
        </article>
      `;
    }).join("");
  }

  function renderLocalVideos(localVideos, config) {
    state.localVideos = localVideos || null;
    ensureLocalInputSelection(localVideos);
    const inputFiles = Array.isArray(localVideos?.input_files) ? localVideos.input_files : [];
    const outputFiles = Array.isArray(localVideos?.output_files) ? localVideos.output_files : [];
    const selectedInputFile = inputFiles.find((item) => norm(item.path) === norm(state.selectedLocalInput)) || null;
    const selectedOutputPath = String(localVideos?.selected_output || config?.output?.output_path || "").trim();
    const selectedOutputFile = outputFiles.find((item) => norm(item.path) === norm(selectedOutputPath)) || null;
    const currentPlayerFile = [...outputFiles, ...inputFiles].find((item) => norm(item.path) === norm(state.localPlayerPath)) || null;
    const isSaveToLocalMode = String(config?.output?.sink || "").trim().toLowerCase() === "ffmpeg_file";
    if (state.localPlayerPath && !currentPlayerFile) clearLocalPlayer();
    setText("local-selected-input", selectedInputFile ? selectedInputFile.name : "未选择");
    setHtml("local-input-list", renderVideoFileList(inputFiles, "input", state.selectedLocalInput, true));
    setHtml("local-output-list", renderVideoFileList(outputFiles, "output", "", false));
    setText("local-video-summary", JSON.stringify({
      root: localVideos?.root || "",
      input_dir: localVideos?.input_dir || "",
      output_dir: localVideos?.output_dir || "",
      selected_input: state.selectedLocalInput || "",
      selected_output: selectedOutputPath,
    }, null, 2));
    if (currentPlayerFile) {
      setLocalPlayer(currentPlayerFile, false);
      return;
    }
    if (selectedOutputFile && isSaveToLocalMode) {
      setLocalPlayer(selectedOutputFile, false);
      return;
    }
    if (!state.localPlayerPath) {
      const fallbackFile = selectedInputFile || inputFiles[0] || outputFiles[0] || null;
      if (fallbackFile) setLocalPlayer(fallbackFile, true);
      else clearLocalPlayer();
    }
  }

  function updateLocalVideoBundle(localVideos) {
    if (state.bundle) state.bundle.local_videos = localVideos || {};
    renderLocalVideos(localVideos || {}, (state.bundle || {}).config || {});
  }

  async function uploadLocalVideo() {
    const input = $("local-video-upload-file");
    const button = $("btn-local-video-upload");
    const file = input?.files?.[0] || null;
    if (!file) {
      alert("请先选择一个本地视频文件。");
      return;
    }
    const formData = new FormData();
    formData.append("file", file, file.name);
    try {
      if (button) button.disabled = true;
      setText("local-video-upload-status", `正在上传：${file.name}`);
      const payload = await fetchJson("/api/local-videos/input", {
        method: "POST",
        body: formData,
      });
      if (payload.item?.path) state.selectedLocalInput = payload.item.path;
      if (input) input.value = "";
      updateLocalVideoBundle(payload.local_videos || {});
      setText("local-video-upload-status", `上传完成：${payload.item?.name || file.name}`);
    } catch (error) {
      setText("local-video-upload-status", `上传失败：${error.message}`);
      alert(`上传视频失败：${error.message}`);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function deleteLocalVideo(file) {
    if (!file) return;
    const folder = String(file.folder || "");
    const name = String(file.name || "");
    const path = String(file.path || "");
    if (!window.confirm(`确定删除 ${folder} / ${name} 吗？如果运行任务正在使用该文件，删除后可能需要重新启动任务。`)) return;
    const fallbackUrl = `/api/local-videos/${encodeURIComponent(folder)}/${encodeURIComponent(name)}`;
    try {
      const payload = await fetchJson(String(file.url || fallbackUrl), {method: "DELETE"});
      if (norm(state.selectedLocalInput) === norm(path)) state.selectedLocalInput = "";
      if (norm(state.localPlayerPath) === norm(path)) clearLocalPlayer();
      updateLocalVideoBundle(payload.local_videos || {});
      setText("local-video-upload-status", `已删除：${folder} / ${name}`);
    } catch (error) {
      alert(`删除视频失败：${error.message}`);
    }
  }

  function renderAlarms(alarms) {
    const container = $("alarm-list");
    if (!container) return;
    const items = Array.isArray(alarms) ? alarms : [];
    if (!items.length) {
      container.innerHTML = '<div class="event-item"><div class="event-topic">暂无报警</div><div class="event-meta">等待识别到目标人脸</div></div>';
      return;
    }

    container.innerHTML = items.map((alarm) => {
      const snapshots = Array.isArray(alarm.snapshots) ? alarm.snapshots : [];
      const grid = snapshots.length
        ? `<div class="alarm-grid">${snapshots.slice().reverse().map((item) => {
            const imageUrl = resolveAlarmImageUrl(item);
            const imageSrc = withCacheBust(imageUrl, item.timestamp_ms);
            return `
            <a href="${esc(imageUrl || "#")}" target="_blank" rel="noreferrer">
              <img class="alarm-image" src="${esc(imageSrc)}" alt="alarm snapshot">
              <div class="alarm-image-time">${esc(ts(item.timestamp_ms || item.captured_at_ms))}</div>
            </a>
          `;
          }).join("")}</div>`
        : '<div class="alarm-meta">暂时还没有抓拍图片。</div>';
      return `
        <div class="alarm-item">
          <div class="alarm-head">
            <div class="alarm-title">#${Number(alarm.alarm_id || 0)} / ${esc(alarm.identity || "unknown")}</div>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              <div class="alarm-state ${alarm.active ? "active" : ""}">${alarm.active ? "进行中" : "已结束"}</div>
              <button class="face-lib-mini-btn danger" data-alarm-action="delete" data-id="${Number(alarm.alarm_id || 0)}" type="button">删除</button>
            </div>
          </div>
          <div class="alarm-meta">出现时间：${esc(ts(alarm.appeared_at_ms))}</div>
          <div class="alarm-meta">离开时间：${alarm.active ? "--" : esc(ts(alarm.left_at_ms))}</div>
          <div class="alarm-meta">持续时长：${Math.floor(Number(alarm.duration_ms || 0) / 1000)} 秒 / 抓拍数量：${Number(alarm.snapshot_count || 0)}</div>
          ${grid}
        </div>
      `;
    }).join("");
  }

  function renderFaceLibraryPaths() {
    const paths = state.faceLibraryPaths || {};
    const summary = state.faceLibrarySummary || {};
    setText("face-lib-paths", [
      `数据库：${paths.database_path || "--"}`,
      `样本目录：${paths.sample_dir || "--"}`,
      `数据库状态：${summary.database_exists ? "可用" : "未创建"}`,
      `身份数量：${Number(summary.identity_count || 0)}`,
      `样本数量：${Number(summary.sample_count || 0)}`,
    ].join("\n"));
  }

  function renderFaceLibraryIdentities() {
    const box = $("face-lib-identity-list");
    if (!box) return;
    if (!state.faceLibraryIdentities.length) {
      box.innerHTML = '<div class="face-lib-empty-cover">人脸库里还没有身份数据。</div>';
      return;
    }

    box.innerHTML = `<div class="face-lib-items">${state.faceLibraryIdentities.map((item) => {
      const active = Number(item.id) === Number(state.selectedIdentityId);
      const imageUrl = resolveIdentityImageUrl(item);
      const cover = imageUrl
        ? `<img class="face-lib-cover" src="${esc(withCacheBust(imageUrl, item.updated_at || Date.now()))}" alt="${esc(item.name)}">`
        : '<div class="face-lib-empty-cover">暂无样本图片</div>';
      const previewButton = imageUrl
        ? `<button class="face-lib-mini-btn" data-face-preview-action="open" data-src="${esc(imageUrl)}" data-title="${esc(`${item.name} 封面`)}" type="button">查看图片</button>`
        : "";
      return `
        <article class="face-lib-item${active ? " active" : ""}" data-face-select="${Number(item.id)}">
          <div class="face-lib-item-head">
            <strong>${esc(item.name)}</strong>
            <span>${Number(item.sample_count || 0)} 个样本 / ${item.enabled ? "已启用" : "已停用"}</span>
          </div>
          ${cover}
          <div class="face-lib-item-meta">备注：${esc(item.note || "无")}</div>
          <div class="face-lib-item-meta">更新时间：${esc(tsIso(item.updated_at))}</div>
          <div class="face-lib-item-actions">
            ${previewButton}
            <button class="face-lib-mini-btn" data-face-identity-action="toggle-enabled" data-id="${Number(item.id)}" data-name="${esc(item.name)}" data-enabled="${item.enabled ? "1" : "0"}" type="button">${item.enabled ? "停用" : "启用"}</button>
            <button class="face-lib-mini-btn danger" data-face-identity-action="delete" data-id="${Number(item.id)}" data-name="${esc(item.name)}" type="button">删除</button>
          </div>
        </article>
      `;
    }).join("")}</div>`;
  }

  function renderFaceLibrarySamples() {
    const box = $("face-lib-sample-list");
    if (!box) return;
    const selected = state.faceLibraryIdentities.find((item) => Number(item.id) === Number(state.selectedIdentityId));
    setText("face-lib-selected-title", `当前选中身份：${selected?.name || "--"}`);
    if (!selected) {
      box.innerHTML = '<div class="face-lib-empty-cover">请先选择一个身份。</div>';
      return;
    }
    if (!state.faceLibrarySamples.length) {
      box.innerHTML = '<div class="face-lib-empty-cover">这个身份暂时还没有样本。</div>';
      return;
    }

    box.innerHTML = `<div class="face-lib-samples">${state.faceLibrarySamples.map((item) => {
      const imageUrl = resolveSampleImageUrl(item);
      const cover = imageUrl
        ? `<img class="face-lib-thumb" src="${esc(withCacheBust(imageUrl, item.created_at || Date.now()))}" alt="sample ${Number(item.id)}">`
        : '<div class="face-lib-empty-cover">暂无样本图片</div>';
      const previewButton = imageUrl
        ? `<button class="face-lib-mini-btn" data-face-preview-action="open" data-src="${esc(imageUrl)}" data-title="${esc(`${selected.name} 样本 #${Number(item.id)}`)}" type="button">查看图片</button>`
        : "";
      return `
      <div class="face-lib-sample">
        ${cover}
        <div class="face-lib-sample-actions">
          ${previewButton}
          <button class="face-lib-mini-btn danger" data-face-sample-action="delete" data-id="${Number(item.id)}" type="button">删除</button>
        </div>
      </div>
    `;
    }).join("")}</div>`;
  }

  function openFormDialog(mode) {
    const dialog = $("face-lib-form-dialog");
    if (!dialog) return;
    const selected = state.faceLibraryIdentities.find((item) => Number(item.id) === Number(state.selectedIdentityId));
    state.dialogMode = mode;
    setText("face-lib-dialog-title", mode === "create" ? "新增身份" : "编辑身份");
    setText("face-lib-dialog-desc", mode === "create" ? "创建一个新身份，并可选上传第一张样本图片。" : "更新身份信息，并可选追加新的样本图片。");
    setValue("face-lib-dialog-name", mode === "edit" ? selected?.name || "" : "");
    setValue("face-lib-dialog-note", mode === "edit" ? selected?.note || "" : "");
    if ($("face-lib-dialog-file")) $("face-lib-dialog-file").value = "";
    dialog.showModal();
  }

  function closeFormDialog() {
    $("face-lib-form-dialog")?.close();
  }

  function openPreviewDialog(src, title) {
    const dialog = $("face-lib-preview-dialog");
    const image = $("face-lib-preview-image");
    if (!dialog || !image || !src || src === "#") return;
    image.src = src;
    image.alt = title || "预览图片";
    setText("face-lib-preview-title", title || "图片预览");
    dialog.showModal();
  }

  function closePreviewDialog() {
    const dialog = $("face-lib-preview-dialog");
    const image = $("face-lib-preview-image");
    if (image) image.removeAttribute("src");
    dialog?.close();
  }

  function fileToDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("读取图片文件失败"));
      reader.readAsDataURL(file);
    });
  }

  async function refreshFaceLibrarySampleList(identityId) {
    try {
      const payload = await fetchJson(`/api/face-library/identities/${Number(identityId)}/samples`);
      state.faceLibrarySamples = Array.isArray(payload.items) ? payload.items : [];
      renderFaceLibrarySamples();
    } catch (error) {
      state.faceLibrarySamples = [];
      setHtml("face-lib-sample-list", `<div class="face-lib-empty-cover">样本加载失败：${esc(error.message)}</div>`);
    }
  }

  async function refreshFaceLibraryIdentityList(options = {}) {
    const preserveSelection = options.preserveSelection !== false;
    const keyword = $("face-lib-search")?.value.trim() || "";
    try {
      const payload = await fetchJson(keyword ? `/api/face-library/identities?q=${encodeURIComponent(keyword)}` : "/api/face-library/identities");
      state.faceLibraryIdentities = Array.isArray(payload.items) ? payload.items : [];
      state.faceLibraryPaths = payload.paths || null;
      state.faceLibrarySummary = payload.summary || null;
      renderFaceLibraryPaths();
      const selectedExists = preserveSelection && state.faceLibraryIdentities.some((item) => Number(item.id) === Number(state.selectedIdentityId));
      if (!selectedExists) state.selectedIdentityId = state.faceLibraryIdentities[0] ? Number(state.faceLibraryIdentities[0].id) : 0;
      renderFaceLibraryIdentities();
      if (state.selectedIdentityId) {
        await refreshFaceLibrarySampleList(state.selectedIdentityId);
      } else {
        state.faceLibrarySamples = [];
        renderFaceLibrarySamples();
      }
    } catch (error) {
      setText("face-lib-paths", `人脸库加载失败：${error.message}`);
      setHtml("face-lib-identity-list", `<div class="face-lib-empty-cover">身份列表加载失败：${esc(error.message)}</div>`);
      setHtml("face-lib-sample-list", '<div class="face-lib-empty-cover">等待样本数据。</div>');
    }
  }

  async function submitFormDialog() {
    const name = $("face-lib-dialog-name")?.value.trim() || "";
    const note = $("face-lib-dialog-note")?.value.trim() || "";
    const file = $("face-lib-dialog-file")?.files?.[0];
    const selectedId = Number(state.selectedIdentityId || 0);
    if (!name) {
      alert("姓名不能为空。");
      return false;
    }

    if (state.dialogMode === "create") {
      const payload = await fetchJson("/api/face-library/identities", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, note}),
      });
      state.selectedIdentityId = Number((payload.item || {}).id || 0);
    } else {
      if (!selectedId) {
        alert("编辑前请先选择一个身份。");
        return false;
      }
      await fetchJson(`/api/face-library/identities/${selectedId}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, note}),
      });
    }

    if (file) {
      const imageBase64 = await fileToDataUrl(file);
      const identityId = Number(state.selectedIdentityId || selectedId || 0);
      if (identityId) {
        await fetchJson(`/api/face-library/identities/${identityId}/samples`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({image_base64: imageBase64}),
        });
      }
    }

    await refreshFaceLibraryIdentityList({preserveSelection: true});
    return true;
  }

  async function uploadFaceSample() {
    if (!state.selectedIdentityId) {
      alert("请先选择一个身份。");
      return;
    }
    const file = $("face-lib-sample-file")?.files?.[0];
    if (!file) {
      alert("请先选择一张图片。");
      return;
    }
    try {
      const imageBase64 = await fileToDataUrl(file);
      await fetchJson(`/api/face-library/identities/${Number(state.selectedIdentityId)}/samples`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({image_base64: imageBase64}),
      });
      if ($("face-lib-sample-file")) $("face-lib-sample-file").value = "";
      await refreshFaceLibraryIdentityList({preserveSelection: true});
    } catch (error) {
      alert(`样本上传失败：${error.message}`);
    }
  }

  async function deleteIdentity(id, name) {
    if (!id) return;
    if (!window.confirm(`确定删除身份“${name || id}”及其全部样本吗？`)) return;
    try {
      await fetchJson(`/api/face-library/identities/${Number(id)}`, {method: "DELETE"});
      if (Number(state.selectedIdentityId) === Number(id)) state.selectedIdentityId = 0;
      await refreshFaceLibraryIdentityList({preserveSelection: true});
    } catch (error) {
      alert(`删除失败：${error.message}`);
    }
  }

  async function deleteAllIdentities() {
    if (!window.confirm("确定删除人脸库中的全部身份吗？")) return;
    try {
      const payload = await fetchJson("/api/face-library/identities");
      const items = Array.isArray(payload.items) ? payload.items : [];
      for (const item of items) {
        await fetchJson(`/api/face-library/identities/${Number(item.id)}`, {method: "DELETE"});
      }
      state.selectedIdentityId = 0;
      await refreshFaceLibraryIdentityList({preserveSelection: false});
    } catch (error) {
      alert(`全部删除失败：${error.message}`);
    }
  }

  async function deleteFaceSample(id) {
    if (!window.confirm(`确定删除样本 #${Number(id)} 吗？`)) return;
    try {
      await fetchJson(`/api/face-library/samples/${Number(id)}`, {method: "DELETE"});
      await refreshFaceLibraryIdentityList({preserveSelection: true});
    } catch (error) {
      alert(`删除样本失败：${error.message}`);
    }
  }

  async function reloadRuntimeForFaceLibrary() {
    if (!window.confirm("确定重载运行时以应用最新的人脸库吗？")) return;
    try {
      await fetchJson("/api/face-library/runtime/reload", {method: "POST"});
      await refreshBootstrap();
    } catch (error) {
      alert(`运行时重载失败：${error.message}`);
    }
  }

  async function deleteAlarm(alarmId) {
    if (!window.confirm(`确定删除报警 #${Number(alarmId)} 吗？`)) return;
    try {
      await fetchJson(`/api/alarms/${Number(alarmId)}`, {method: "DELETE"});
      await refreshBootstrap();
    } catch (error) {
      alert(`删除报警失败：${error.message}`);
    }
  }

  async function deleteAllAlarms() {
    if (!window.confirm("确定删除全部报警信息和抓拍图片吗？")) return;
    try {
      await fetchJson("/api/alarms", {method: "DELETE"});
      await refreshBootstrap();
    } catch (error) {
      alert(`删除全部报警失败：${error.message}`);
    }
  }

  async function toggleFaceIdentityEnabled(id, enabled) {
    try {
      await fetchJson(`/api/face-library/identities/${Number(id)}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: !Boolean(enabled)}),
      });
      await Promise.all([
        refreshFaceLibraryIdentityList({preserveSelection: true}),
        refreshBootstrap(),
      ]);
    } catch (error) {
      alert(`身份使能切换失败：${error.message}`);
    }
  }

  function renderRuntime(bundle) {
    state.bundle = bundle || {};
    const runtime = state.bundle.runtime || {};
    const config = state.bundle.config || {};
    const input = config.input || {};
    const output = config.output || {};
    const streams = state.bundle.streams || {};
    const source = (state.bundle.sources || [])[0] || {};

    state.configPresets = Array.isArray(state.bundle.config_presets) ? state.bundle.config_presets.slice() : [];
    const visibleConfigPath = syncConfigSelection(runtime.config_path || "");
    setRuntimeBadge(runtime.state || "stopped");
    renderAlarmToggle(config);
    renderFaceRecognitionToggle(config);
    setText("service-mediamtx", (runtime.services?.mediamtx || {}).state || "未知");
    setText("service-mediamtx-host", (runtime.services?.mediamtx || {}).enabled ? `${runtime.services.mediamtx.host || "127.0.0.1"}:${runtime.services.mediamtx.port || 8554}` : "未启用");
    setText("service-websocket", state.bundle.websocket_enabled ? "已启用" : "未启用");
    renderMetrics(runtime.metrics || {}, runtime.source_id || source.source_id || "");
    renderSystem(state.bundle.system || {});
    renderCameraInfo(runtime, config, source);
    renderDebug(runtime.metrics || {}, runtime.source_id || source.source_id || "", state.bundle.system || {}, config);
    renderPreview(streams);
    renderStreamSummary(streams);
    renderAlarms(state.bundle.alarms || []);
    renderLocalVideos(state.bundle.local_videos || {}, config);

    const localPanel = $("local-start-panel");
    if (localPanel) {
      localPanel.classList.toggle("hidden", !isLocalVideoConfig(config) && !isLocalVideoPreset(findPresetByPath(visibleConfigPath || $("config-preset")?.value || "")));
    }
  }

  async function refreshAlarms() {
    try {
      const payload = await fetchJson("/api/alarms?limit=100");
      renderAlarms(payload.alarms || []);
    } catch (_error) {
      // Keep current alarm panel if refresh fails.
    }
  }

  function pushEvent(event) {
    const topic = String(event?.topic || "");
    if (!topic) return;
    if (topic.startsWith("alarm.face.")) refreshAlarms().catch(() => {});
  }

  async function refreshBootstrap() {
    renderRuntime(await fetchJson("/api/frontend/bootstrap"));
  }

  function startPolling() {
    if (state.pollingTimer) window.clearInterval(state.pollingTimer);
    state.pollingTimer = window.setInterval(() => {
      refreshBootstrap().catch((error) => setRealtimeMode(`轮询失败：${error.message}`));
    }, 2000);
  }

  function connectEventSource() {
    if (state.eventSource) state.eventSource.close();
    try {
      const source = new EventSource("/api/stream?limit=10");
      state.eventSource = source;
      source.onopen = () => setRealtimeMode("SSE 已连接");
      source.onmessage = async (message) => {
        try {
          const payload = JSON.parse(message.data);
          pushEvent(payload);
          if (LOW_FREQUENCY_REFRESH_TOPICS.has(String(payload.topic || ""))) {
            await refreshBootstrap().catch(() => {});
          }
        } catch (error) {
          console.warn("SSE parse failed", error);
        }
      };
      source.onerror = () => setRealtimeMode("SSE 已断开");
    } catch (error) {
      setRealtimeMode(`SSE 不可用：${error.message}`);
    }
  }

  function connectWebSocket() {
    if (!window.WebSocket) {
      connectEventSource();
      return;
    }
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    state.websocket = ws;
    ws.onopen = () => setRealtimeMode("WebSocket 已连接");
    ws.onmessage = (message) => {
      try {
        const payload = JSON.parse(message.data);
        if (payload.type === "snapshot") {
          renderRuntime(payload.payload || {});
          return;
        }
        if (payload.type === "heartbeat") return;
        pushEvent(payload);
        if (LOW_FREQUENCY_REFRESH_TOPICS.has(String(payload.topic || ""))) {
          refreshBootstrap().catch(() => {});
        }
      } catch (error) {
        console.warn("WebSocket parse failed", error);
      }
    };
    ws.onclose = () => {
      setRealtimeMode("WebSocket 已断开，正在切换到 SSE");
      connectEventSource();
    };
    ws.onerror = () => ws.close();
  }

  async function startRuntime() {
    const configPath = $("config-path")?.value.trim() || "";
    const preset = findPresetByPath(configPath);
    const payload = {config_path: configPath || null};
    if (isLocalVideoPreset(preset) || isLocalVideoConfig(state.bundle?.config)) {
      if (!state.selectedLocalInput) {
        alert("请先在本地视频页签里选择一个输入视频。");
        return;
      }
      payload.local_video_input = state.selectedLocalInput;
    }
    try {
      await fetchJson("/api/runtime/start", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      state.pendingConfigPath = configPath;
      state.configSelectionDirty = false;
      await refreshBootstrap();
    } catch (error) {
      alert(`启动失败：${error.message}`);
    }
  }

  async function stopRuntime() {
    try {
      await fetchJson("/api/runtime/stop", {method: "POST"});
      await refreshBootstrap();
    } catch (error) {
      alert(`停止失败：${error.message}`);
    }
  }

  async function toggleAlarmEnabled() {
    try {
      await fetchJson("/api/runtime/alarm", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: !Boolean(state.alarmEnabled)}),
      });
      await refreshBootstrap();
    } catch (error) {
      alert(`报警开关切换失败：${error.message}`);
    }
  }

  async function toggleFaceRecognitionEnabled() {
    try {
      await fetchJson("/api/runtime/face-recognition", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: !Boolean(state.faceRecognitionEnabled)}),
      });
      await refreshBootstrap();
    } catch (error) {
      alert(`人脸识别开关切换失败：${error.message}`);
    }
  }

  function bindTabs() {
    document.querySelectorAll(".stage-tab").forEach((button) => {
      button.addEventListener("click", () => {
        const view = button.dataset.view || "preview";
        document.querySelectorAll(".stage-tab").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
        document.querySelectorAll(".stage-view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
        if (view === "alarms") refreshAlarms().catch(() => {});
        if (view === "inspector") refreshFaceLibraryIdentityList({preserveSelection: true}).catch(() => {});
      });
    });
  }

  function bindLocalVideoBoard() {
    $("btn-local-video-upload")?.addEventListener("click", () => {
      uploadLocalVideo().catch((error) => alert(`上传视频失败：${error.message}`));
    });
    $("local-video-upload-file")?.addEventListener("change", (event) => {
      const file = event.target?.files?.[0] || null;
      setText(
        "local-video-upload-status",
        file ? `待上传：${file.name}（${formatFileSize(file.size)}）` : "支持 mp4 / mkv / mov / avi / ts / m4v / webm",
      );
    });

    $("local-input-list")?.addEventListener("click", (event) => {
      const card = event.target.closest("[data-path]");
      if (!card) return;
      const path = card.dataset.path || "";
      const files = Array.isArray(state.localVideos?.input_files) ? state.localVideos.input_files : [];
      const file = files.find((item) => norm(item.path) === norm(path));
      if (!file) return;
      const action = event.target.closest("[data-action]")?.dataset.action || "preview";
      if (action === "delete-video") {
        deleteLocalVideo(file).catch((error) => alert(`删除视频失败：${error.message}`));
        return;
      }
      if (action === "use-input") {
        state.selectedLocalInput = file.path;
        renderLocalVideos(state.localVideos || {}, (state.bundle || {}).config || {});
      }
      setLocalPlayer(file, true);
    });

    $("local-output-list")?.addEventListener("click", (event) => {
      const card = event.target.closest("[data-path]");
      if (!card) return;
      const path = card.dataset.path || "";
      const files = Array.isArray(state.localVideos?.output_files) ? state.localVideos.output_files : [];
      const file = files.find((item) => norm(item.path) === norm(path));
      if (!file) return;
      const action = event.target.closest("[data-action]")?.dataset.action || "preview";
      if (action === "delete-video") {
        deleteLocalVideo(file).catch((error) => alert(`删除视频失败：${error.message}`));
        return;
      }
      setLocalPlayer(file, true);
    });
  }

  function bindFaceLibraryActions() {
    $("face-lib-create")?.addEventListener("click", () => openFormDialog("create"));
    $("face-lib-edit")?.addEventListener("click", () => {
      if (!state.selectedIdentityId) {
        alert("请先选择一个身份。");
        return;
      }
      openFormDialog("edit");
    });
    $("face-lib-refresh")?.addEventListener("click", () => refreshFaceLibraryIdentityList({preserveSelection: true}).catch((error) => alert(`刷新失败：${error.message}`)));
    $("face-lib-delete-all")?.addEventListener("click", deleteAllIdentities);
    $("btn-alarm-delete-all")?.addEventListener("click", deleteAllAlarms);
    $("face-lib-guide")?.addEventListener("click", () => $("face-lib-guide-dialog")?.showModal());
    $("face-lib-guide-close")?.addEventListener("click", () => $("face-lib-guide-dialog")?.close());
    $("face-lib-dialog-cancel")?.addEventListener("click", closeFormDialog);
    $("face-lib-preview-close")?.addEventListener("click", closePreviewDialog);
    $("face-lib-preview-dialog")?.addEventListener("click", (event) => {
      if (event.target?.id === "face-lib-preview-dialog") closePreviewDialog();
    });
    $("face-lib-upload")?.addEventListener("click", uploadFaceSample);
    $("face-lib-reload-runtime")?.addEventListener("click", reloadRuntimeForFaceLibrary);
    $("face-lib-search")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        refreshFaceLibraryIdentityList({preserveSelection: true}).catch((error) => alert(`搜索失败：${error.message}`));
      }
    });

    $("face-lib-identity-list")?.addEventListener("click", (event) => {
      const previewButton = event.target.closest("button[data-face-preview-action]");
      if (previewButton && previewButton.dataset.facePreviewAction === "open") {
        openPreviewDialog(previewButton.dataset.src || "", previewButton.dataset.title || "图片预览");
        return;
      }
      const deleteButton = event.target.closest("button[data-face-identity-action]");
      if (deleteButton && deleteButton.dataset.faceIdentityAction === "delete") {
        deleteIdentity(Number(deleteButton.dataset.id || 0), deleteButton.dataset.name || "");
        return;
      }
      if (deleteButton && deleteButton.dataset.faceIdentityAction === "toggle-enabled") {
        toggleFaceIdentityEnabled(Number(deleteButton.dataset.id || 0), String(deleteButton.dataset.enabled || "") === "1");
        return;
      }
      const card = event.target.closest("[data-face-select]");
      if (!card) return;
      const id = Number(card.dataset.faceSelect || 0);
      if (!id) return;
      state.selectedIdentityId = id;
      renderFaceLibraryIdentities();
      refreshFaceLibrarySampleList(id).catch((error) => alert(`样本加载失败：${error.message}`));
    });

    $("face-lib-sample-list")?.addEventListener("click", (event) => {
      const previewButton = event.target.closest("button[data-face-preview-action]");
      if (previewButton && previewButton.dataset.facePreviewAction === "open") {
        openPreviewDialog(previewButton.dataset.src || "", previewButton.dataset.title || "图片预览");
        return;
      }
      const deleteButton = event.target.closest("button[data-face-sample-action]");
      if (deleteButton && deleteButton.dataset.faceSampleAction === "delete") {
        deleteFaceSample(Number(deleteButton.dataset.id || 0));
      }
    });

    $("alarm-list")?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-alarm-action]");
      if (!button) return;
      if (button.dataset.alarmAction === "delete") {
        deleteAlarm(Number(button.dataset.id || 0));
      }
    });

    $("face-lib-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const ok = await submitFormDialog();
        if (ok) closeFormDialog();
      } catch (error) {
        alert(`人脸库更新失败：${error.message}`);
      }
    });
  }

  function bindActions() {
    $("btn-start")?.addEventListener("click", startRuntime);
    $("btn-stop")?.addEventListener("click", stopRuntime);
    $("btn-refresh")?.addEventListener("click", () => {
      Promise.all([
        refreshBootstrap(),
        refreshFaceLibraryIdentityList({preserveSelection: true}).catch(() => {}),
      ]).catch((error) => alert(`刷新失败：${error.message}`));
    });
    $("btn-alarm-toggle")?.addEventListener("click", toggleAlarmEnabled);
    $("btn-face-recognition-toggle")?.addEventListener("click", toggleFaceRecognitionEnabled);
    $("config-preset")?.addEventListener("change", (event) => {
      const path = event.target.value || "";
      rememberPendingConfigPath(path);
      setValue("config-path", path);
      renderPresetOptions(path);
    });
    $("config-path")?.addEventListener("input", (event) => {
      rememberPendingConfigPath(event.target.value || "");
      renderPresetOptions(event.target.value || "");
    });

    $("stream-video")?.addEventListener("error", () => {
      const overlay = $("preview-overlay");
      if (!overlay) return;
      overlay.style.display = "flex";
      overlay.textContent = "浏览器播放 HLS 预览失败，请使用下方 RTSP/HLS/WebRTC 地址。";
    });
    $("stream-frame")?.addEventListener("load", () => {
      const overlay = $("preview-overlay");
      if (overlay) overlay.style.display = "none";
    });
    $("local-video-player")?.addEventListener("error", (event) => {
      const player = event.currentTarget;
      const fallbackUrl = String(player?.dataset?.fallbackUrl || "");
      if (!player || !fallbackUrl) return;
      if (String(player.currentSrc || "").includes(fallbackUrl)) return;
      player.src = fallbackUrl;
      player.load();
    });

    bindTabs();
    bindLocalVideoBoard();
    bindFaceLibraryActions();
  }

  async function main() {
    localizeStaticText();
    bindActions();
    await refreshBootstrap();
    await refreshFaceLibraryIdentityList({preserveSelection: true});
    connectWebSocket();
    startPolling();
  }

  main().catch((error) => setRealtimeMode(`初始化失败：${error.message}`));
})();
