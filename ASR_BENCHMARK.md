# 百炼 ASR 本机测速

日期：2026-11-18（北京时间）。仅发送系统合成测试语音，无真实会议内容。

## 方法

- 同一段 11.354625 秒中文语音，macOS Tingting 合成；16 kHz、单声道、16-bit PCM WAV。
- 文本：大家好，今天我们测试会议语音识别速度。请在明天下午三点之前完成接口测试，并把结果发送给项目负责人。
- 北京地域，使用 dashscope.aliyuncs.com。HTTP 非流式；WebSocket 使用各模型官方协议。
- 每个模型完整音频模式各测试 3 次，包含网络连接、上传及最终响应时间。
- 两个实时模型另各测试 3 次：每 100 ms 发送音频，记录开始推音频至首次非空文本、最后音频发送至会话完成的延迟。旧版使用 Manual 模式；新版使用默认流式配置。不是统一 VAD 参数的对照。
- 首次出字指中间结果，不代表最终稳定文本；实时模式总时间包含播放音频的时间，不应与整段上传耗时直接排名。
- 小样本、本机网络、单一合成语音，不代表通用性能或准确率评测。

## 完整音频转写（秒）

| 展示名称 | API 模型名 | 第1次 | 第2次 | 第3次 | 中位数 |
|---|---|---:|---:|---:|---:|
| Flash | qwen3-asr-flash | 0.817 | 0.859 | 0.803 | **0.817** |
| Flash Realtime | qwen3-asr-flash-realtime | 0.886 | 0.907 | 0.871 | 0.886 |
| Audio 3.1 Flash | qwen-audio-3.1-asr-flash | 1.782 | 0.888 | 0.715 | 0.888 |
| Audio 3.1 Flash 实时版 | qwen-audio-3.1-asr-flash-streaming | 2.369 | 6.728 | 2.870 | 2.870 |

官方现行文档将 Audio 3.1 的持续实时识别型号称为 `streaming`，而不是 `realtime`。本次按这一型号测试。

## 模拟实时推流（秒，中位数）

| 模型 | 开始推音频至首次文本 | 音频发送完至会话完成 |
|---|---:|---:|
| qwen3-asr-flash-realtime | 6.441 | **0.245** |
| qwen-audio-3.1-asr-flash-streaming | **0.412** | 0.356 |

## 选择

- 现有项目按停顿切 WAV 再请求：选 **qwen3-asr-flash**，本次整段模式中位耗时最短，且波动小。
- 会议边说边显示字幕：推荐 **qwen-audio-3.1-asr-flash-streaming**，本次首字延迟明显更低。需要真正流式采集、服务端 WebSocket 代理与前端中间字幕渲染；只替换现有 HTTP 转写函数无法获得这个实时延迟。
- 本项目现在两路都留着，页面开关切：默认本地（`/transcribe`，LazyTyper），切到百炼走流式
  （`bailian_stream.py` + 浏览器 WebSocket 上行，模型 `qwen-audio-3.1-asr-flash-streaming`），
  见 README「工作方式」。

## 官方参考

- 模型选型：https://help.aliyun.com/zh/model-studio/asr-model
- Flash HTTP：https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference
- Flash Realtime：https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-interaction-process
- Audio 3.1 Flash HTTP：https://help.aliyun.com/zh/model-studio/qwen-audio-asr-http-api
- Audio 3.1 实时 WebSocket：https://help.aliyun.com/zh/model-studio/qwen-audio-asr-streaming-websocket-api

## 文件与复现

- 测速脚本：`benchmark_asr.py`，需要 `websocket-client`（本机已安装）。通过环境变量 `DASHSCOPE_API_KEY` 或标准输入传入密钥，不写入代码。
- 原始响应：`/tmp/bailian-benchmark-results.json`。
- 测试音频：`/tmp/bailian-benchmark.wav`。
- 实时测试结束时会关闭 WebSocket，不保持计费会话。
