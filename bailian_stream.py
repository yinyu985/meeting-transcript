"""Streaming ASR for Aliyun Bailian (DashScope) Qwen-Audio-3.1-ASR-Flash-Streaming.

Two layers:

* ``BailianStream``  — one WebSocket task: connect -> feed PCM -> finish.
* ``StreamSession``  — long-running, meeting-length wrapper. Restarts the task
  after an unexpected upstream close and rotates it at sentence boundaries, so a
  drop costs at most a fraction of a sentence instead of the rest of the meeting.

Callback contract (``on_event``):

    {"type": "final", "text": "...", "at": <epoch ms>, "ms": <duration>, "id": 3}
    {"type": "draft", "text": "...", "at": ..., "ms": 0, "id": 3}
    {"type": "reconnecting", "error": "..."} / {"type": "reconnected"}
    {"type": "error", "error": "..."}

Audio leaves the machine (dashscope.aliyuncs.com).
"""

import collections
import json
import threading
import time
import uuid

import websocket

URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
MODEL = "qwen-audio-3.1-asr-flash-streaming"


class StreamError(RuntimeError):
    pass


class BailianStream:
    """One recognition task: connect -> feed PCM -> finish."""

    def __init__(self, api_key, on_event, language="", model=MODEL, sample_rate=16000,
                 on_close=None):
        self.api_key = api_key
        self.on_event = on_event
        self.on_close = on_close
        self.language = language
        self.model = model
        self.sample_rate = sample_rate
        self.task_id = uuid.uuid4().hex
        self.ws = None
        self.reader = None
        self.started = threading.Event()
        self.finished = threading.Event()
        self.failure = None
        self.stop_requested = False
        self.send_lock = threading.Lock()
        self.audio_seconds = 0.0
        self.audio_bytes = 0
        self.history = collections.deque()   # (offset, bytes)，用于轮换/断线后重放
        self.history_bytes = 0
        self.last_final_end_ms = 0
        self.last_event_at = None

    # ---------------------------------------------------------------- lifecycle
    def connect(self, timeout=10):
        try:
            self.ws = websocket.create_connection(
                URL,
                header={"Authorization": "Bearer " + self.api_key},
                timeout=timeout,
                suppress_origin=True,
            )
            # 没有事件时（纯静音）靠心跳报平安；留足余量，别把安静当断线。
            self.ws.settimeout(45)
        except Exception as exc:
            raise StreamError(f"无法连接百炼：{exc}") from exc

        # heartbeat=true：会议里长时间没人说话，服务端不会把连接判成空闲超时。
        parameters = {"sample_rate": self.sample_rate, "format": "pcm", "heartbeat": True}
        if self.language:
            parameters["language_hints"] = [self.language]
        self._send({
            "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": parameters,
                "input": {},
            },
        })
        self.reader = threading.Thread(target=self._read_loop, name="asr-reader", daemon=True)
        self.reader.start()
        if not self.started.wait(timeout):
            self.abort()
            raise StreamError(self.failure or "百炼会话启动超时")
        if self.failure:
            self.abort()
            raise StreamError(self.failure)

    def feed(self, pcm):
        """Append raw PCM16LE mono bytes. Returns True when accepted."""
        if not pcm:
            return True
        self._remember(pcm)
        self.audio_seconds += len(pcm) / (self.sample_rate * 2)
        try:
            with self.send_lock:
                self.ws.send_binary(pcm)
            return True
        except Exception as exc:
            self.failure = self.failure or f"音频发送失败：{exc}"
            return False

    def finish(self, timeout=20):
        """Ask for a final sentence, wait for task-finished."""
        if self.ws is None:
            return
        self.stop_requested = True
        try:
            self._send({
                "header": {"action": "finish-task", "task_id": self.task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            })
        except Exception as exc:
            self.failure = self.failure or f"结束指令发送失败：{exc}"
            return
        self.finished.wait(timeout)

    def abort(self):
        self.stop_requested = True
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- internals
    def _remember(self, pcm):
        self.history.append((self.audio_bytes, pcm))
        self.history_bytes += len(pcm)
        self.audio_bytes += len(pcm)
        limit = self.sample_rate * 2 * 120  # 保留最近两分钟
        while self.history_bytes > limit and len(self.history) > 1:
            _, old = self.history.popleft()
            self.history_bytes -= len(old)

    def audio_since(self, ms, max_ms=30000):
        """Bytes sent at or after ``ms`` into this task (used to replay un-finalised audio)."""
        start_ms = max(int(ms), self.audio_bytes // (self.sample_rate * 2) * 1000 - max_ms, 0)
        start = max(0, min(self.audio_bytes, start_ms * (self.sample_rate * 2) // 1000))
        out = []
        for offset, chunk in self.history:
            end = offset + len(chunk)
            if end <= start:
                continue
            out.append(chunk[start - offset:] if offset < start else chunk)
        return b"".join(out)

    def _send(self, payload):
        with self.send_lock:
            self.ws.send(json.dumps(payload))

    def _read_loop(self):
        detail = None
        try:
            while True:
                raw = self.ws.recv()
                if not raw:
                    detail = "远端关闭连接"
                    break
                msg = json.loads(raw)
                self.last_event_at = time.time()
                event = msg.get("header", {}).get("event", "")
                if event == "task-started":
                    self.started.set()
                elif event == "result-generated":
                    self._emit(msg.get("payload") or {})
                elif event == "task-failed":
                    header = msg.get("header", {})
                    detail = header.get("error_message") or "百炼转写任务失败"
                    self.failure = detail
                    break
                elif event == "task-finished":
                    break
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.failure = self.failure or f"接收失败：{exc}"
        finally:
            self.started.set()
            self.finished.set()
            if not self.stop_requested and self.on_close is not None:
                self.on_close(self, detail or "连接结束")

    def _emit(self, payload):
        output = payload.get("output") or {}
        sentence = output.get("sentence") or output.get("output", {}).get("sentence") or {}
        if sentence.get("heartbeat"):
            return
        text = (sentence.get("text") or "").strip()
        if not text:
            return
        final = bool(sentence.get("sentence_end"))
        begin = sentence.get("begin_time")
        end = sentence.get("end_time")
        if final and end:
            self.last_final_end_ms = max(self.last_final_end_ms, int(end))
        self.on_event({
            "id": sentence.get("sentence_id"),
            "text": text,
            "final": final,
            "begin": begin,
            "end": end,
        })


class StreamSession:
    """Meeting-length stream: survives upstream drops, rotates at sentence end."""

    def __init__(self, api_key, on_event, language="", rotate_after=None,
                 max_buffer_seconds=10.0):
        self.api_key = api_key
        self.on_event = on_event
        self.language = language
        # 实测服务端没有可预期的单任务时长上限（330 秒连跑 0 次断连），
        # 因此默认不主动轮换：轮换重放会带来重复或合并句子的副作用。
        self.rotate_after = rotate_after
        self.max_buffer = int(max_buffer_seconds * 32000)
        self.lock = threading.Lock()
        self.task = None
        self.pending = []
        self.pending_bytes = 0
        self.dropped_bytes = 0
        self.audio_offset = 0.0     # seconds already consumed by finished tasks
        self.started_ms = None
        self.task_seconds = 0.0
        self.reconnects = 0
        self.rotations = 0
        self.rotation_seconds = 0.0
        self.rotating = False
        self.fatal = None
        self.start()

    # ---------------------------------------------------------------- public
    def start(self):
        self._connect(first=True)

    def feed(self, pcm):
        if not pcm or self.fatal:
            return not self.fatal
        with self.lock:
            if self.started_ms is None:
                self.started_ms = round(time.time() * 1000)
            task = self.task
            if task is not None:
                self.rotation_seconds += len(pcm) / 32000
                return task.feed(pcm)
            # 断连/轮换期间先缓存，恢复后补发；积压太多就丢最旧的，保持实时。
            if self.pending_bytes + len(pcm) > self.max_buffer:
                while self.pending and self.pending_bytes + len(pcm) > self.max_buffer:
                    self.dropped_bytes += len(self.pending.pop(0))
                    self.pending_bytes = sum(len(x) for x in self.pending)
            self.pending.append(pcm)
            self.pending_bytes += len(pcm)
            return True

    def finish(self, timeout=20):
        with self.lock:
            task, self.task = self.task, None
        if task is None:
            return
        task.finish(timeout)
        with self.lock:
            self.audio_offset += task.audio_seconds
            self.task_seconds = 0.0
        task.abort()

    def abort(self):
        with self.lock:
            task, self.task = self.task, None
        if task is not None:
            task.abort()

    def stats(self):
        with self.lock:
            return {"audio_s": round(self.audio_offset + self.task_seconds, 1),
                    "reconnects": self.reconnects,
                    "rotations": self.rotations,
                    "dropped_s": round(self.dropped_bytes / 32000, 2)}

    # -------------------------------------------------------------- internals
    def _connect(self, first=False, notice=True):
        last = None
        for attempt in range(6):
            task = BailianStream(self.api_key, self._task_event, self.language,
                                 on_close=self._task_closed)
            try:
                task.connect(timeout=10)
            except StreamError as exc:
                last = exc
                time.sleep(min(1 + attempt, 5))
                continue
            with self.lock:
                self.task = task
                self.task_seconds = 0.0
                buffered = b"".join(self.pending)
                self.pending.clear()
                self.pending_bytes = 0
            if buffered:
                task.feed(buffered)
            if not first and notice:
                self.on_event({"type": "reconnected"})
            return
        self.fatal = str(last)
        self.on_event({"type": "error", "error": f"转写服务连接失败：{last}"})

    def _task_event(self, ev):
        begin = int(ev.get("begin") or 0)
        end = int(ev.get("end") or begin)
        with self.lock:
            offset = self.audio_offset
            started = self.started_ms
            self.task_seconds = self.task.audio_seconds if self.task else self.task_seconds
        at = (started or round(time.time() * 1000)) + round(offset * 1000) + begin
        self.on_event({
            "id": ev.get("id"),
            "text": ev["text"],
            "final": ev["final"],
            "at": at,
            "ms": max(0, end - begin),
        })
        # 轮换放到独立线程：读线程不能等自己的 task-finished。
        if ev.get("final") and self._should_rotate() and not self.rotating:
            threading.Thread(target=self._rotate, name="asr-rotate", daemon=True).start()

    def _should_rotate(self):
        if not self.rotate_after:
            return False
        with self.lock:
            return self.rotation_seconds >= self.rotate_after

    def _rotate(self):
        """Restart on a sentence boundary so a long meeting never hits per-task limits.

        The model finalises a sentence a few seconds behind real time, so audio for the
        next sentence may already be in the old task. Replay it instead of losing it.
        """
        with self.lock:
            if self.rotating:
                return
            self.rotating = True
            task, self.task = self.task, None
        try:
            if task is None:
                return
            # 从"最后一句定稿的结尾"重放：不能用来触发的那个事件的时间，
            # 它可能是重放音频里的旧句子。
            replay = task.audio_since(task.last_final_end_ms)
            # 刚拿到本句定稿，直接断开即可；在这里等 task-finished 会等自己。
            task.abort()
            with self.lock:
                self.audio_offset += task.audio_seconds - len(replay) / 32000
                self.rotation_seconds = len(replay) / 32000
                if replay:
                    self.pending.insert(0, replay)
                    self.pending_bytes += len(replay)
            self.rotations += 1
            self._connect(notice=False)
        finally:
            self.rotating = False

    def _task_closed(self, task, detail):
        with self.lock:
            if self.task is not task:
                return
            self.task = None
            # 没定稿的音频重放一次：宁可偶尔重复几个字，也别整句丢掉。
            replay = task.audio_since(task.last_final_end_ms)
            self.audio_offset += task.audio_seconds - len(replay) / 32000
            if replay:
                self.pending.insert(0, replay)
                self.pending_bytes += len(replay)
            self.reconnects += 1
        self.on_event({"type": "reconnecting", "error": detail})
        threading.Thread(target=self._connect, name="asr-reconnect", daemon=True).start()