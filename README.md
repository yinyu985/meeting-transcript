# 会议转录 / Meeting Transcript

浏览器里的实时会议转录。麦克风和会议应用的声音分两路采集，句子带说话时刻，
转录结果存本机。页面顶部有一个**引擎开关**，只有两档：

| 引擎 | 链路 | 特点 |
|---|---|---|
| 本地 | 浏览器内 VAD 断句 → 16k WAV → 本机 LazyTyper | 音频不出网；每句停顿后整段出字，慢一些 |
| 百炼 | 16k PCM 持续上行 → 百炼流式 ASR | 边说边出字，低延迟；音频上传阿里云 |

```
麦克风 ────────────┐                  ┌─ 本地：VAD 断句 → WAV → LazyTyper
                   ├─▶ 16k 音频 ──────┤
指定应用（窗口）──┘                  └─ 百炼：PCM → WebSocket → 流式识别
```

- **我**：来自麦克风
- **对方**：来自你共享的那个应用/窗口

说话人靠**输入通道**区分，不靠声纹猜测，所以对方那边有几个人不区分。

> **隐私**：本地引擎的音频不出这台电脑。切到百炼后音频会上传到
> `dashscope.aliyuncs.com`。本项目不保存音频，只在本机留转写文本和历史。

## 前置条件

| 需要 | 说明 |
|---|---|
| macOS + Chrome / Chromium（Helium 等） | 共享音频依赖浏览器的屏幕/窗口共享能力 |
| [LazyTyper](https://lazytyper.com) 桌面版（用本地引擎时） | 保持运行；页面只调它的本地 HTTP 接口 |
| 百炼 API Key（用百炼引擎时） | `DASHSCOPE_API_KEY` 环境变量，或密钥文件（见下） |
| `websocket-client`（用百炼引擎时） | `python3 -m pip install websocket-client` |

两个引擎互不依赖：缺 `websocket-client` 或没配 Key 时服务端照常启动，本地模式可用。

## 快速开始

```bash
git clone https://github.com/yinyu985/meeting-transcript.git
cd meeting-transcript
python3 -m pip install websocket-client   # 只用本地引擎可跳过
python3 server.py            # http://localhost:8787
```

然后打开页面，顶部「引擎」选 **本地** 或 **百炼**：

- 没存过选择时：本地能用就用本地，本地没起来就用百炼
- 每次点「开始」前重新探测两路；选中的那路不可用而另一路可用时，本次自动切换并提示
- 录音中不能切换（按钮置灰），停止后再改

音频输入（右上角两行开关）：

1. **麦克风输入**：默认开启，记为「我」。
2. **指定应用输入**：打开后浏览器弹出共享框，选「窗口」和目标应用（腾讯会议、
   Zoom、浏览器标签页都行），并把音频共享打开。记为「对方」。

然后点主界面的「开始」。

必须是 `localhost` 或 `127.0.0.1`：`getUserMedia` 只在安全上下文可用。

### API Key 放哪

服务端按顺序查找，浏览器永远拿不到 Key：

1. 环境变量 `DASHSCOPE_API_KEY`
2. `~/Library/Application Support/meeting-transcript/bailian.key`（推荐，在仓库外）
3. 项目目录 `.bailian.key`（已在 `.gitignore` 里）

```bash
dir="$HOME/Library/Application Support/meeting-transcript"
mkdir -p "$dir"
umask 077 && printf '%s\n' 'sk-…' > "$dir/bailian.key"
chmod 600 "$dir/bailian.key"
```

## 两个输入是独立开关

| 操作 | 结果 |
|---|---|
| 录音中关掉某一路 | 立即停止该路采集与上行，另一路继续 |
| 浏览器里点「停止共享」 | 应用那一路自动关闭，开关同步变灰 |
| 两路全关 | 点「开始」会提示先开一路 |
| 点主界面「停止」 | 先把尾段断完再断开，下次需重新授权窗口共享 |

## 工作方式

### 采集（浏览器内）

`AudioWorklet` 把音频降到 16 kHz 单声道，每 20 ms 一块，主线程算 RMS 驱动电平表。
之后两条路分开：

- **本地**：帧进 VAD 做切片，凑够一段编码成 WAV 再 POST `/transcribe`
- **百炼**：帧直接转 16-bit PCM，推 WebSocket `/api/stream`

浏览器 `fetch` 的流式请求体只在 HTTP/2 下可用，本机静态服务走 HTTP/1.1，
所以流式那条用 WebSocket。

### 本地转写（`/transcribe`）

断句规则：

- 阈值 = `max(0.006, 噪声底 × 2.5)`，噪声底在空闲帧上做 EMA，会适应房间
- 连续 3 帧超阈值判定开口，前 300 ms 缓存一起带上，不吃字头
- 录够 2 秒后，遇到 240 ms 停顿就提交；10 秒后改为 200 ms，20 秒后改为 160 ms，不再缩短
- 不足 2 秒的短句仍在静音 700 ms 后提交；手动停止时保留更短的尾音

`server.py` 把 WAV 转发给 LazyTyper 的 `/v1/audio/transcriptions`，端口从
`~/Library/Application Support/com.lazytyper.desktop/server.port` 每次现读，App 重启换端口不用改配置。
LazyTyper 对带 `Origin` 头的请求返回 403，浏览器不能直连，转发必须在服务端。

### 百炼转写（`/api/stream`）

`server.py` 把 PCM 转发给百炼流式识别，把事件简化成两种：

- `draft`：本句还在变，前端原地刷新同一段文字
- `final`：本句定稿，落成一段，并写入本地历史

**为什么需要这层代理**：API Key 不能放在浏览器里；顺带把百炼的事件格式收敛成
两个状态，前端不用理解它的 `sentence_end` / `word.fixed` 细节。

模型：`qwen-audio-3.1-asr-flash-streaming`（见 `ASR_BENCHMARK.md` 的选型对比）。
本机实测（2026-11-20，11 秒中文合成语音、100 ms 一帧上行）：首个文字 **0.42 秒**，
`audio_s` 与服务端收到的字节数一致。

### 断线与背压

上游（百炼）偶发直接关闭 TCP：日志里没有任何 `task-failed`，关闭前 0.0 秒还有正常结果。
实测同一模型连续会话 92.6 秒、330 秒都不出问题，61 秒那次断开属于偶发，
不是单次请求时长上限，也不是静音超时（当时正在连续说话）。

应对方式是不假设连接稳定：

- 服务端自己重建任务：断连后把「最后一句定稿之后」的音频重放给新任务，
  已定稿的句子不重复，未定稿的话不会整句丢
- 浏览器只看到状态栏「转写线路重连中」，不弹错误、不中断会议
- `heartbeat: true`：长时间没人说话时，服务端不会把连接判成空闲超时
- 不做主动轮换：实测没有需要绕过的时长上限，轮换反而会重放音频、出现重复或合并句子
- 上行拥塞超过约 2 秒时只丢弃新音频帧，采集和电平表不阻塞
- 停止时先发 `stop`，等服务端把最后一句断完再断开

服务端为每条流记一行结束日志，便于事后判断：
`{"event":"stream","audio_s":…,"upstream_restarts":0,"dropped_s":0.0,"failure":null}`。
连续会话里偶尔一句被模型并成长句（例如 16 秒的一句），这是模型自身断句，不是断线。

### 界面

- 每段两行：人物 + 时间一行小字，正文另起一行；百炼模式的草稿段用虚线边框半透明表示未定稿
- 正文短句收在屏幕中间，长句自动吃满宽度
- 底部 2 px 音量线随电平伸缩，停止录音后消失
- 「复制」导出 `HH:MM:SS  我：正文`

设置面板：引擎（本地 / 百炼）、字号 15–32 px、行距、按人物给字幕配色、
麦克风 / 指定应用开关、语言提示（自动 / 中文 / EN），偏好存 `localStorage`。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/app.js` `/history.js` `/recorder-worklet.js` | 页面 |
| GET | `/api/health` | `{"engines":{"local":{…},"bailian":{…}}}`，两路各自报可用性 |
| POST | `/transcribe` | 本地模式：WAV 上行，转写结果 JSON |
| WS | `/api/stream?channel=mic\|remote&session=…&lang=…` | 百炼模式：上行二进制 PCM；下行 JSON 文本帧 `ready/draft/final/warning/done/error` |
| GET/POST/DELETE | `/api/history…` | 本地历史 |

## 目录

```
server.py              静态服务 + 两个引擎的代理（本地 HTTP / 百炼 WebSocket）
bailian_stream.py      百炼流式 ASR 客户端（websocket-client）
ws_server.py           极简 RFC 6455 服务端：握手与帧
index.html             页面结构与样式
app.js                 采集、两种转写管线、草稿与定稿渲染
recorder-worklet.js    降采样到 16k、切 20ms 帧
history_store.py       本地 SQLite 历史
requirements.txt       百炼引擎的 Python 依赖
benchmark_asr.py       四个模型的测速脚本
ASR_BENCHMARK.md        测速结果与选型理由
```

## 已知限制

- **百炼引擎的音频会上云**，本地引擎不会。速度是拿这个换来的。
- **本地引擎慢**。本机模型每句要等停顿后才开始转写，句子越长等待越明显。
- **macOS 上浏览器不一定支持「只录某个应用」**。共享框里若写的是「系统音频」，
  其他应用的声音也可能一起进来；选窗口 + 打开音频共享通常最接近只录该应用。
- **扬声器外放会造成重复**。对方声音从扬声器出来，麦克风也会拾到一份。戴耳机，
  或关掉麦克风那一路。
- **浏览器共享提示无法关闭**。
- 按「停止」时若正说到一半，最后一句会以当时听到的内容定稿，可能缺字。
- 只转写，不做会议纪要总结。要总结需另接 LLM。
- 一条流式会话一直用到会议结束；若服务端侧长连接被断，会自动重连，
  但断点处那一句可能重复或缺失。

## 回退

两条链路都在同一份代码里，页面开关切换即可，不需要换分支或回到旧版本。

## 许可

代码 MIT，见 `LICENSE`。百炼引擎的转写模型由阿里云百炼提供，按百炼的计费与条款执行；
本地引擎使用你本机安装的 LazyTyper 模型。