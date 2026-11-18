// 麦克风 / 共享应用音频 -> 独立 VAD -> 本地转写。
// 两个输入独立开关；按来源固定标记「我 / 对方」。

const FRAME_MS = 20;
const RATE = 16000;
const KEY = "meeting.transcript.v5";

const cfg = {
  startFrames: 3,
  hangoverMs: 700,
  prerollMs: 300,
  minMs: 300,
  maxMs: 20000,
  paragraphGapMs: 1800,
  paragraphMaxMs: 60000,
};

const el = {
  paper: document.getElementById("paper"),
  empty: document.getElementById("empty"),
  stage: document.getElementById("stage"),
  record: document.getElementById("record"),
  stateText: document.getElementById("stateText"),
  clock: document.getElementById("clock"),
  pressure: document.querySelector("#pressure i"),
  copy: document.getElementById("copy"),
  clearWrap: document.getElementById("clearWrap"),
  jump: document.getElementById("jump"),
  toast: document.getElementById("toast"),
  fontVal: document.getElementById("fontVal"),
  lineVal: document.getElementById("lineVal"),
  smaller: document.getElementById("smaller"),
  larger: document.getElementById("larger"),
  tighter: document.getElementById("tighter"),
  looser: document.getElementById("looser"),
  colorSpk: document.getElementById("colorSpk"),
  micInput: document.getElementById("micInput"),
  appInput: document.getElementById("appInput"),
  micStatus: document.getElementById("micStatus"),
  appStatus: document.getElementById("appStatus"),
};

const state = {
  lines: [],          // [{ t, text, kind }]
  lang: "",
  recording: false,
  stopping: false,
  ctx: null,
  channels: new Map(), // 'mic' | 'remote' -> channel
  startedAt: 0,
  tick: null,
  chain: Promise.resolve(),
  generation: 0,
  sessionId: null,
  pending: 0,
  pendingMs: 0,
  samples: [],
  segmentMs: 20000,
  overload: false,
  stick: true,
  micEnabled: true,
  inputStreams: new Map(),
};

const rms = (f) => {
  let s = 0;
  for (let i = 0; i < f.length; i++) s += f[i] * f[i];
  return Math.sqrt(s / f.length);
};

const clockOf = (t) =>
  new Date(t).toLocaleTimeString("zh-CN", {
    hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

// 身份就来自输入通道，不再猜声纹
const whoLabel = (entry) =>
  entry.kind === "remote" ? "对方" : entry.kind === "mic" ? "我" : "未知来源";

// ---------- 渲染 ----------
function paragraph(entry) {
  const p = document.createElement("p");
  p.dataset.spk = entry.kind === "remote" ? "1" : "0";

  const meta = document.createElement("span");
  meta.className = "meta";

  const spk = document.createElement("span");
  spk.className = "spk";
  spk.textContent = whoLabel(entry);

  const time = document.createElement("time");
  time.dateTime = new Date(entry.t).toISOString();
  time.textContent = clockOf(entry.t);

  meta.append(spk, time);

  const body = document.createElement("span");
  body.className = "text";
  body.textContent = entry.text;

  p.append(meta, body);
  return p;
}

function render() {
  el.paper.replaceChildren();
  for (const line of state.lines) el.paper.append(paragraph(line));
  if (state.recording) attachCaret();
  el.empty.style.display = state.lines.length ? "none" : "";
  el.copy.disabled = state.lines.length === 0;
  syncClear();
  syncLabels();
  layoutParagraphs();
}

// 小 / 中 / 大逐档试排，选能容纳三行的最窄档；大档仍有限宽。
// 超过大档容量时自然增加行数，绝不为了压到三行而铺满屏幕。
function layoutParagraphs() {
  const paragraphs = [...el.paper.querySelectorAll('p')];
  paragraphs.forEach(p => p.classList.remove('medium', 'large'));
  const exceedsThreeLines = p => {
    const text = p.querySelector('.text');
    const lineHeight = parseFloat(getComputedStyle(text).lineHeight);
    return text.getBoundingClientRect().height > lineHeight * 3 + 1;
  };
  const medium = paragraphs.filter(exceedsThreeLines);
  medium.forEach(p => p.classList.add('medium'));
  const large = medium.filter(exceedsThreeLines);
  large.forEach(p => {
    p.classList.remove('medium');
    p.classList.add('large');
  });
}

let layoutFrame;
function scheduleParagraphLayout() {
  cancelAnimationFrame(layoutFrame);
  layoutFrame = requestAnimationFrame(() => {
    layoutParagraphs();
    if (state.stick) scrollToLatest(true);
  });
}
addEventListener('resize', scheduleParagraphLayout);
document.fonts?.ready.then(scheduleParagraphLayout);

function syncLabels() {
  document.body.classList.toggle("diar", state.lines.length > 0);
}

function attachCaret() {
  document.querySelectorAll(".caret").forEach((n) => n.remove());
  const last = el.paper.querySelector("p:last-of-type .text");
  if (!last) return;
  const c = document.createElement("span");
  c.className = "caret";
  last.append(c);
}

function persist() {
  try {
    localStorage.setItem(KEY, JSON.stringify(state.lines));
  } catch {}
}

function push(text, at, extra = {}) {
  // 两路可同时讲话；只合并采集时确定的同一来源段落。
  const previous = extra.group && state.lines.find(l => l.group === extra.group);
  if (previous) {
    const space = /[a-z0-9]$/i.test(previous.text) && /^[a-z0-9]/i.test(text) ? " " : "";
    previous.text += space + text;
    previous.end = extra.end;
  } else {
    state.lines.push({ t: at || Date.now(), text, ...extra });
    state.lines.sort((a, b) => a.t - b.t);
  }
  render();
  el.empty.style.display = "none";
  el.copy.disabled = false;
  syncClear();
  syncLabels();
  if (state.recording) attachCaret();
  if (state.stick) scrollToLatest();
  persist();
}

// ---------- 滚动 ----------
function nearBottom() {
  const s = el.stage;
  return s.scrollHeight - s.scrollTop - s.clientHeight < 90;
}

function scrollToLatest(instant) {
  el.stage.scrollTo({ top: el.stage.scrollHeight, behavior: instant ? "auto" : "smooth" });
  state.stick = true;
  el.jump.classList.remove("show");
}

el.stage.addEventListener("scroll", () => {
  state.stick = nearBottom();
  el.jump.classList.toggle("show", !state.stick && state.lines.length > 0);
});

el.jump.onclick = () => scrollToLatest();

// ---------- 音频 -> WAV ----------
function encodeWav(list) {
  const n = list.reduce((a, f) => a + f.length, 0);
  const view = new DataView(new ArrayBuffer(44 + n * 2));
  const tag = (o, s) => [...s].forEach((c, i) => view.setUint8(o + i, c.charCodeAt(0)));
  tag(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, RATE, true);
  view.setUint32(28, RATE * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); tag(36, "data"); view.setUint32(40, n * 2, true);
  let o = 44;
  for (const f of list) {
    for (let i = 0; i < f.length; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, f[i]));
      view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
  }
  return new Blob([view.buffer], { type: "audio/wav" });
}

// ---------- 两路音频通道 ----------
function createChannel(id, stream) {
  return {
    id,
    stream,
    node: null,
    inSpeech: false,
    frames: [],
    preroll: [],
    above: 0,
    below: 0,
    noise: 0.003,
    segAt: 0,
    level: 0,
    origin: 0,
    elapsed: 0,
    group: null,
    groupAt: 0,
    lastVoiceEnd: -Infinity,
    continuing: false,
  };
}

async function attachChannel(ctx, id, stream) {
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "recorder");
  const ch = createChannel(id, stream);
  ch.node = node;
  node.port.onmessage = (e) => {
    if (e.data?.type === "drained") { ch.onDrained?.(); return; }
    if (state.recording) onFrame(ch, e.data);
  };
  src.connect(node);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  node.connect(mute).connect(ctx.destination);
  state.channels.set(id, ch);

  // 输入流的 ended 事件由 registerInput() 统一收尾。
  return ch;
}

// 必须直接在点击开关的调用栈里请求，保留浏览器的用户手势。
function requestAppAudio() {
  return navigator.mediaDevices.getDisplayMedia({
    video: { displaySurface: "window", frameRate: { ideal: 1, max: 2 } },
    audio: true,
    windowAudio: "window",
    systemAudio: "include",
    selfBrowserSurface: "exclude",
    surfaceSwitching: "exclude",
  });
}

function requestMicAudio() {
  return navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
}

function onFrame(ch, frame) {
  if (!ch.origin) ch.origin = Date.now() - FRAME_MS;
  ch.elapsed += frame.length / RATE * 1000;
  const now = ch.origin + ch.elapsed;
  const level = rms(frame);
  ch.level = level;

  let maxLevel = 0;
  state.channels.forEach((c) => (maxLevel = Math.max(maxLevel, c.level)));
  el.pressure.style.transform = `scaleX(${Math.min(1, maxLevel * 14)})`;

  const thr = Math.max(0.006, ch.noise * 2.5);

  if (!ch.inSpeech) {
    if (level < thr) ch.noise = ch.noise * 0.95 + level * 0.05;
    ch.preroll.push(frame);
    if (ch.preroll.length > cfg.prerollMs / FRAME_MS) ch.preroll.shift();
    ch.above = level > Math.max(0.006, ch.noise * 2.5) ? ch.above + 1 : 0;
    if (ch.above >= cfg.startFrames) {
      ch.inSpeech = true;
      ch.above = ch.below = 0;
      ch.frames = ch.preroll.slice();
      ch.preroll = [];
      ch.segAt = now - ch.frames.length * FRAME_MS;
      const voiceAt = now - cfg.startFrames * FRAME_MS;
      if (!ch.group || voiceAt - ch.lastVoiceEnd > cfg.paragraphGapMs ||
          voiceAt - ch.groupAt >= cfg.paragraphMaxMs) {
        ch.group = crypto.randomUUID();
        ch.groupAt = ch.segAt;
      }
      ch.lastVoiceEnd = now;
    }
    return;
  }

  ch.frames.push(frame);
  ch.below = level < thr ? ch.below + 1 : 0;
  if (level >= thr) ch.lastVoiceEnd = now;
  const ms = ch.frames.reduce((n, f) => n + f.length, 0) / RATE * 1000;
  if (ch.below * FRAME_MS >= cfg.hangoverMs) flush(ch, ms, "silence");
  else if (ms >= (state.segmentMs || cfg.maxMs)) flush(ch, ms, "limit");
}

function flush(ch, ms, reason = "stop") {
  const captured = ch.frames;
  if (!captured.length) return;
  const at = ch.segAt;
  const end = at + ms;
  const meta = { group: ch.group, end, reason, continuation: ch.continuing };
  ch.frames = [];
  ch.inSpeech = reason === "limit";
  ch.continuing = reason === "limit";
  ch.above = 0;
  if (reason === "limit") {
    // 下一帧直接接续，不重新等待开口，不遗漏切点附近的声音。
    ch.segAt = end;
    if (end - ch.groupAt >= cfg.paragraphMaxMs) {
      ch.group = crypto.randomUUID();
      ch.groupAt = end;
    }
  } else ch.below = 0;
  if (ms < cfg.minMs && !meta.continuation && reason !== "stop") return;
  send(encodeWav(captured), at, ms, ch.id, meta);
}

// ---------- 性能与背压：观测实际请求，不假定模型支持高效并发 ----------
function updateProcessingStatus() {
  const backlog = state.pending ? ` · 待转写 ${Math.ceil(state.pendingMs / 1000)} 秒` : "";
  if (!state.recording) {
    el.stateText.textContent = state.pending ? `正在完成转写${backlog}` : (state.lines.length ? "已停止" : "待机");
  } else {
    el.stateText.textContent = `录音中${state.channels.size ? "" : " · 无输入"}${backlog}`;
  }
  el.stateText.title = `切片上限 ${state.segmentMs / 1000} 秒；待转写包含当前请求，不是预计等待时间`;
}

function observePerformance(durationMs, elapsedMs) {
  state.samples.push({ durationMs, elapsedMs });
  if (state.samples.length > 16) state.samples.shift();
  // 初次请求可能冷启动；至少四次请求后才调整，且不持久化旧机器/模型数据。
  if (state.samples.length < 4) return;
  const recent = state.samples.slice(1);
  const rtf = recent.reduce((n, s) => n + s.elapsedMs, 0) /
    recent.reduce((n, s) => n + s.durationMs, 0);
  // 仅在明显有余量时降低延迟；双路留足余量，积压时回到保守上限。
  const target = state.pendingMs > 30000 || rtf > 0.35 ? 20000 : rtf < 0.15 ? 8000 : 12000;
  state.segmentMs = Math.max(target, state.segmentMs - 4000);
}

// ---------- 发送 ----------
function send(blob, at, dur, chanId, meta = {}) {
  const generation = state.generation;
  const sessionId = state.sessionId;
  const id = crypto.randomUUID();
  const queued = performance.now();
  const language = state.lang;
  state.pending++;
  state.pendingMs += dur;
  updateProcessingStatus();
  // 保留已采集内容，仅停止新增采集。用 microtask 避免 flush/stop 递归。
  if (state.pendingMs >= 120000 && state.recording && !state.overload) {
    state.overload = true;
    queueMicrotask(async () => {
      if (state.recording) await stop();
      toast("待转写音频超过两分钟，已停止采集；正在处理已录内容。请使用更快的模型后再开始。", true);
    });
  }
  console.info("audio.segment", { id, chanId, at, dur, ...meta });
  state.chain = state.chain.then(async () => {
    const began = performance.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120000);
    try {
      // 清空只清除当前显示；已经录下的内容仍转写并归档。
      if (generation !== state.generation && !sessionId) return;
      const r = await fetch("/transcribe", {
        method: "POST",
        headers: {
          "Content-Type": "audio/wav", "X-Language": language,
          "X-Segment-Id": id, "X-Channel": chanId,
          "X-Session-Id": sessionId || "",
          "X-Audio-Start": String(at), "X-Audio-Ms": String(dur),
          "X-Cut-Reason": meta.reason || "unknown",
          "X-Queue-Ms": String(Math.round(performance.now() - queued)),
        },
        body: blob,
        signal: controller.signal,
      });
      const data = await r.json();
      if (!r.ok || data.error || typeof data.text !== "string") {
        throw new Error(data.error || `HTTP ${r.status}`);
      }
      if (data.history_error) toast(data.history_error, true);
      observePerformance(dur, performance.now() - began);
      const text = data.text.trim();
      if (!text) return;
      if (generation === state.generation) push(text, at, { kind: chanId, ...meta });
    } catch (err) {
      console.error("audio.segment.failed", { id, chanId, error: String(err) });
      toast(`转写失败（${chanId} ${clockOf(at)}）：${err.message || err}`, true);
    } finally {
      clearTimeout(timeout);
      state.pending--;
      state.pendingMs = Math.max(0, state.pendingMs - dur);
      updateProcessingStatus();
      console.info("audio.segment.done", { id, elapsedMs: performance.now() - began, queueMs: began - queued });
    }
  });
}

// ---------- 生命周期 ----------
async function start() {
  if (state.overload && state.pending) {
    toast("请等积压转写完成后再开始", true);
    return;
  }
  state.overload = false;
  el.record.disabled = true;
  el.stateText.textContent = "开启中…";
  try {
    await openChannels();
    const session = await historyRequest('/api/history', { method: 'POST' });
    state.sessionId = session.id;
  } finally {
    el.record.disabled = false;
  }

  state.recording = true;
  state.startedAt = Date.now();
  state.tick = setInterval(tickClock, 500);
  tickClock();

  document.body.classList.add("rec");
  el.record.lastChild.textContent = "停止";
  syncInputs();
  el.empty.style.display = "none";
  attachCaret();
  scrollToLatest(true);
}

async function openChannels() {
  if (!state.micEnabled && !state.inputStreams.has("remote")) {
    throw new Error("请先在设置中打开麦克风输入或指定应用输入");
  }
  if (state.micEnabled && !state.inputStreams.has("mic")) {
    registerInput("mic", await requestMicAudio());
  }
  const ctx = new AudioContext({ sampleRate: RATE });
  state.ctx = ctx;
  await ctx.resume();
  await ctx.audioWorklet.addModule("/recorder-worklet.js");
  for (const [id, stream] of state.inputStreams) {
    await attachChannel(ctx, id, stream);
  }
  syncInputs();
}

async function stop() {
  if (state.stopping) return;
  state.stopping = true;
  el.record.disabled = true;
  el.micInput.disabled = el.appInput.disabled = true;
  el.stateText.textContent = "正在提交尾段…";
  await Promise.all([...state.channels.values()].map(ch => new Promise(resolve => {
    const timeout = setTimeout(() => {
      ch.onDrained = null;
      toast("音频缓存交付超时，尾音可能不完整", true);
      resolve();
    }, 2000);
    ch.onDrained = () => { clearTimeout(timeout); ch.onDrained = null; resolve(); };
    ch.node.port.postMessage({ type: "drain" });
  })));
  state.channels.forEach((ch) => {
    // 如果按停止时刚开口，还未满足连续三帧，也保留这段候选声音。
    if (!ch.inSpeech && ch.above > 0 && ch.preroll.length) {
      ch.frames = ch.preroll;
      ch.segAt = ch.origin + ch.elapsed - ch.frames.reduce((n, f) => n + f.length, 0) / RATE * 1000;
      ch.group = crypto.randomUUID();
    }
    if (ch.frames.length) flush(ch, ch.frames.reduce((n, f) => n + f.length, 0) / RATE * 1000, "stop");
    ch.inSpeech = false;
  });
  const sessionId = state.sessionId;
  state.sessionId = null;
  await teardownChannels();
  if (sessionId) historyRequest(`/api/history/${sessionId}`, { method: 'POST' })
    .catch(err => toast(`历史时长保存失败：${err.message}`, true));

  state.recording = false;
  clearInterval(state.tick);
  state.tick = null;

  document.body.classList.remove("rec");
  el.record.lastChild.textContent = "开始";
  updateProcessingStatus();
  el.clock.textContent = "00:00";
  el.pressure.style.transform = "scaleX(0)";
  document.querySelectorAll(".caret").forEach((n) => n.remove());
  el.empty.style.display = state.lines.length ? "none" : "";
  state.stopping = false;
  el.record.disabled = false;
  el.micInput.disabled = el.appInput.disabled = false;
}

async function teardownChannels() {
  state.channels.forEach((ch) => {
    ch.node?.port.close();
    ch.node?.disconnect();
  });
  state.channels.clear();
  state.inputStreams.forEach(stream => stream.getTracks().forEach(t => t.stop()));
  state.inputStreams.clear();
  state.sessionDuo = false;
  syncInputs();
  if (state.ctx) {
    try {
      await state.ctx.close();
    } catch {}
    state.ctx = null;
  }
}

function tickClock() {
  const s = Math.floor((Date.now() - state.startedAt) / 1000);
  el.clock.textContent =
    String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

el.record.onclick = async () => {
  try {
    if (state.recording) await stop();
    else await start();
  } catch (err) {
    const denied = err.name === "NotAllowedError" || err.name === "SecurityError";
    el.stateText.textContent = "待机";
    el.empty.style.display = state.lines.length ? "none" : "";
    await teardownChannels();
    toast(
      denied
        ? "麦克风被拒绝。先看地址栏的权限图标，再看 系统设置 → 隐私与安全性 → 麦克风 里的浏览器开关。"
        : "启动失败：" + (err.message || err),
      true
    );
  }
};

// ---------- 复制：时间 + 人物 + 正文 ----------
el.copy.onclick = async () => {
  const text = state.lines
    .map((l) => `${clockOf(l.t)}  ${whoLabel(l)}：${l.text}`)
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制全文");
  } catch {
    toast("复制失败，浏览器拒绝了剪贴板访问", true);
  }
};

// ---------- 清空：两步确认 ----------
let armed = false;


function mkBtn(label, onClick, opts = {}) {
  const b = document.createElement("button");
  b.className = "tbtn";
  b.textContent = label;
  if (opts.danger) b.id = "clearYes";
  if (opts.disabled) b.disabled = true;
  b.onclick = onClick;
  return b;
}

function renderClear(mode) {
  armed = mode === "confirm";
  if (!el.clearWrap.firstChild) {
    const main = mkBtn("清空", () => armed ? doClear() : renderClear("confirm"));
    const cancel = mkBtn("取消", () => { renderClear("idle"); main.focus(); });
    cancel.classList.add("cancel");
    cancel.id = "clearCancel";
    main.setAttribute("aria-controls", cancel.id);
    el.clearWrap.append(main, cancel);
  }
  const [main, cancel] = el.clearWrap.children;
  main.textContent = armed ? "确认清空" : "清空";
  main.id = armed ? "clearYes" : "clearButton";
  main.disabled = !armed && state.lines.length === 0;
  main.setAttribute("aria-expanded", String(armed));
  cancel.hidden = !armed;
}

function syncClear() {
  if (!armed) renderClear("idle");
}

function doClear() {
  state.generation++;
  state.lines = [];
  try {
    localStorage.removeItem(KEY);
  } catch {}
  renderClear("idle");
  render();
  toast("当前字幕已清空，本地历史记录保留");
}

addEventListener("keydown", (e) => {
  if (e.key === "Escape" && armed) {
    renderClear("idle");
    el.clearWrap.firstChild.focus();
  }
});

// ---------- 字号 ----------
const PX_MIN = 15;
const PX_MAX = 32;
let bodyPx = 19;

function applyBody(px) {
  bodyPx = Math.min(PX_MAX, Math.max(PX_MIN, Math.round(px)));
  document.documentElement.style.setProperty("--body", bodyPx + "px");
  el.fontVal.textContent = bodyPx + " px";
  el.smaller.disabled = bodyPx <= PX_MIN;
  el.larger.disabled = bodyPx >= PX_MAX;
  scheduleParagraphLayout();
  try {
    localStorage.setItem("meeting.bodyPx", String(bodyPx));
  } catch {}
  if (state.stick) scrollToLatest(true);
}

el.smaller.onclick = () => applyBody(bodyPx - 1);
el.larger.onclick = () => applyBody(bodyPx + 1);

// ---------- 行距 ----------
const LH_MIN = 1.0;
const LH_MAX = 2.25;
let lineH = 1.85;

function applySpacing(lh) {
  lineH = Math.min(LH_MAX, Math.max(LH_MIN, Math.round(lh * 100) / 100));
  document.documentElement.style.setProperty("--lh", String(lineH));
  el.lineVal.textContent = lineH.toFixed(2);
  el.tighter.disabled = lineH <= LH_MIN;
  el.looser.disabled = lineH >= LH_MAX;
  scheduleParagraphLayout();
  try {
    localStorage.setItem("meeting.lineHeight", String(lineH));
  } catch {}
  if (state.stick) scrollToLatest(true);
}

el.tighter.onclick = () => applySpacing(lineH - 0.1);
el.looser.onclick = () => applySpacing(lineH + 0.1);

// ---------- 按人物配色 ----------
function applyColors(on) {
  document.body.classList.toggle("colorspk", on);
  el.colorSpk.setAttribute("aria-checked", String(on));
  try {
    localStorage.setItem("meeting.colorSpk", on ? "1" : "0");
  } catch {}
}

el.colorSpk.onclick = () => applyColors(!document.body.classList.contains("colorspk"));

// ---------- 两个独立输入开关 ----------
function syncInputs() {
  const appStream = state.inputStreams.get("remote");
  el.micInput.setAttribute("aria-checked", String(state.micEnabled));
  el.appInput.setAttribute("aria-checked", String(!!appStream));
  el.micStatus.textContent = state.micEnabled
    ? (state.recording && state.channels.has("mic") ? "采集中" : "已开启 · 待转写") : "已关闭";
  el.appStatus.textContent = appStream
    ? `${state.recording && state.channels.has("remote") ? "采集中" : "已连接"} · ${appStream.getVideoTracks()[0]?.label || "共享窗口"}`
    : "已关闭 · 打开以选择共享来源";
  el.appStatus.title = el.appStatus.textContent;
  if (state.recording) {
    updateProcessingStatus();
  }
}

function registerInput(id, stream) {
  state.inputStreams.set(id, stream);
  stream.getTracks().forEach(track => track.addEventListener("ended", () => {
    if (state.inputStreams.get(id) !== stream) return;
    disableInput(id);
  }));
  syncInputs();
}

function disableInput(id) {
  const ch = state.channels.get(id);
  if (ch) {
    if (ch.inSpeech) flush(ch, ch.frames.length * FRAME_MS);
    ch.node.port.close();
    ch.node.disconnect();
    state.channels.delete(id);
  }
  const stream = state.inputStreams.get(id);
  state.inputStreams.delete(id);
  stream?.getTracks().forEach(t => t.stop());
  if (id === "mic") {
    state.micEnabled = false;
    localStorage.setItem("meeting.micEnabled", "0");
  }
  syncInputs();
}

async function toggleInput(id) {
  const button = id === "mic" ? el.micInput : el.appInput;
  if ((id === "mic" && state.micEnabled) || state.inputStreams.has(id)) {
    disableInput(id);
    return;
  }
  button.disabled = true;
  let stream;
  try {
    // 在任何 await 之前弹出共享请求，避免丢失用户手势。
    const pending = id === "mic" ? requestMicAudio() : requestAppAudio();
    stream = await pending;
    if (!stream.getAudioTracks().length) {
      throw new Error("没有共享音频。请选择窗口并打开音频共享选项");
    }
    if (id === "mic") {
      state.micEnabled = true;
      localStorage.setItem("meeting.micEnabled", "1");
    }
    registerInput(id, stream);
    if (state.recording) await attachChannel(state.ctx, id, stream);
  } catch (err) {
    stream?.getTracks().forEach(t => t.stop());
    disableInput(id);
    toast(`输入未开启：${err.message || err}`, true);
  } finally {
    button.disabled = false;
    syncInputs();
  }
}

el.micInput.onclick = () => toggleInput("mic");
el.appInput.onclick = () => toggleInput("remote");

// ---------- 语言 ----------
document.querySelectorAll(".seg[role='group'] [data-lang]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-lang]").forEach((x) =>
      x.setAttribute("aria-pressed", String(x === b))
    );
    state.lang = b.dataset.lang;
  };
});

// ---------- toast ----------
let toastTimer;
function toast(msg, bad) {
  el.toast.textContent = msg;
  el.toast.classList.toggle("bad", !!bad);
  el.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.remove("show"), bad ? 6000 : 2000);
}

// ---------- 启动 ----------
const savedPx = Number(localStorage.getItem("meeting.bodyPx"));
applyBody(Number.isFinite(savedPx) && savedPx > 0 ? savedPx : innerWidth < 640 ? 17 : 19);

const savedLh = Number(localStorage.getItem("meeting.lineHeight"));
applySpacing(Number.isFinite(savedLh) && savedLh > 0 ? savedLh : 1.85);

applyColors(localStorage.getItem("meeting.colorSpk") === "1");

try {
  const saved = JSON.parse(localStorage.getItem(KEY) || "[]");
  if (Array.isArray(saved)) {
    state.lines = saved
      .filter((x) => x && typeof x.text === "string")
      .map((x) => ({
        t: Number(x.t) || Date.now(),
        text: x.text,
        kind: x.kind === "mic" || x.kind === "remote" ? x.kind : "mic",
      }));
  }
  state.micEnabled = localStorage.getItem("meeting.micEnabled") !== "0";
} catch {}

syncInputs();
render();
if (state.lines.length) requestAnimationFrame(() => scrollToLatest(true));

// ---------- 后端能力探测 ----------
fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    const lt = h.lazytyper || {};
    if (!lt.ok) toast(`LazyTyper 未就绪：${lt.error || "启动 App 后刷新页面"}`, true);
  })
  .catch(() => toast("代理不可用", true));

if (!navigator.mediaDevices?.getUserMedia) {
  toast("这个页面不是安全上下文，拿不到麦克风。用 http://localhost:8787 打开。", true);
}