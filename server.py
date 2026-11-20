#!/usr/bin/env python3
"""Meeting transcriber: serves the page and proxies ASR, two engines, one switch.

    python3 server.py                 # http://localhost:8787

* local   : browser VAD cuts WAV, this server forwards to LazyTyper's local
            /v1/audio/transcriptions. Nothing leaves the machine.
* bailian : browser streams PCM over WebSocket to /api/stream, this server talks
            to wss://dashscope.aliyuncs.com. Audio leaves the machine. Needs an
            API key (DASHSCOPE_API_KEY or a key file) and websocket-client.

The page picks the engine; each request carries its own path, so both can be
live at the same time without any server-side mode switch.
"""

import argparse
import http.client
import json
import os
import pathlib
import threading
import time
import uuid

import history_store
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 百炼引擎是可选的：缺 websocket-client 时本地模式必须照常能跑。
try:
    import bailian_stream
    import ws_server
except ImportError as exc:
    bailian_stream = ws_server = None
    BAILIAN_IMPORT_ERROR = "缺少依赖 %s：python3 -m pip install websocket-client" % exc.name
else:
    BAILIAN_IMPORT_ERROR = ""

HERE = pathlib.Path(__file__).resolve().parent
SUPPORT = pathlib.Path.home() / "Library/Application Support/com.lazytyper.desktop"
PORT_FILE = SUPPORT / "server.port"
CONFIG_DIR = pathlib.Path.home() / "Library/Application Support/meeting-transcript"
KEY_FILES = (CONFIG_DIR / "bailian.key", HERE / ".bailian.key")
DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
MAX_BODY = 40 * 1024 * 1024
CHANNELS = ("mic", "remote")

def api_key():
    """Server-side only: the browser never sees the key."""
    if DASHSCOPE_KEY:
        return DASHSCOPE_KEY
    for path in KEY_FILES:
        try:
            value = path.read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


# ---------------------------------------------------------------- lazytyper
# 本地引擎。LazyTyper 每次重启会换端口，所以端口文件每次现读。
def upstream_port():
    try:
        return int(PORT_FILE.read_text().strip())
    except Exception:
        return None


def transcribe(wav_bytes, language=""):
    port = upstream_port()
    if not port:
        raise RuntimeError(f"LazyTyper 端口文件不存在: {PORT_FILE}")

    boundary = "----meetingdemo7f3a"
    parts = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode(),
        wav_bytes,
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="response_format"\r\n\r\njson'.encode(),
    ]
    if language:
        parts.append(
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\n{language}'.encode()
        )
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    conn.request(
        "POST",
        "/v1/audio/transcriptions",
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"LazyTyper HTTP {resp.status}: {raw[:300]!r}")
    return json.loads(raw)


def lazytyper_ok():
    port = upstream_port()
    if not port:
        return {"ok": False, "error": f"port file not found: {PORT_FILE}"}
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return {"ok": resp.status == 200, "port": port}
    except Exception as exc:
        return {"ok": False, "port": port, "error": str(exc)}


def bailian_ok():
    if bailian_stream is None:
        return {"ok": False, "error": BAILIAN_IMPORT_ERROR}
    ok = bool(api_key())
    return {"ok": ok, "model": bailian_stream.MODEL,
            "error": "" if ok else "未配置 DASHSCOPE_API_KEY"}


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/history.js": ("history.js", "text/javascript; charset=utf-8"),
    "/recorder-worklet.js": ("recorder-worklet.js", "text/javascript; charset=utf-8"),
}


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 只保留有诊断价值的失败请求，避免音频帧把日志刷满。
        path = getattr(self, "path", "")
        failed = " 200 " not in str(fmt % args)
        if failed and (path.startswith("/transcribe") or path.startswith("/api/stream")):
            print("  %s" % (fmt % args), flush=True)

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------- streaming ASR
    def stream_asr(self, raw_path):
        """浏览器持续推 PCM；服务端把定稿/草稿作为 JSON 文本帧回传。"""
        query = raw_path.partition("?")[2]
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        channel = params.get("channel", "mic")
        if channel not in CHANNELS:
            self._send(400, {"error": "未知音频通道"})
            return
        key = api_key()
        if bailian_stream is None:
            self._send(503, {"error": BAILIAN_IMPORT_ERROR})
            return
        if not key:
            self._send(503, {"error": "未配置百炼 API Key，见 README「在线模型」"})
            return

        sid = (params.get("session") or self.headers.get("X-Session-Id") or "").strip()
        if sid:
            try:
                if not history_store.exists(sid):
                    raise ValueError("历史记录不存在")
            except Exception as exc:
                self._send(400, {"error": str(exc)})
                return

        try:
            ws = ws_server.accept(self)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return

        lock = threading.Lock()
        alive = threading.Event()
        alive.set()

        def emit(event):
            if not alive.is_set():
                return
            with lock:
                try:
                    ws.send_text(json.dumps(event, ensure_ascii=False))
                except Exception:
                    alive.clear()

        holder = {}

        def on_event(ev):
            kind = ev.get("type")
            if kind:
                if kind in ("reconnecting", "reconnected"):
                    # 上游断连由服务端自己重建，浏览器只看到一句状态提示，不打断会议。
                    emit({"type": kind})
                    print(json.dumps({"event": "upstream." + kind, "channel": channel,
                                      "detail": ev.get("error", "")}, ensure_ascii=False), flush=True)
                    return
                if kind == "error":
                    emit({"type": "error", "error": ev.get("error") or "转写服务错误"})
                    return
                return
            text = (ev.get("text") or "").strip()
            if not text:
                return
            at = int(ev.get("at") or 0)
            ms = int(ev.get("ms") or 0)
            emit({"id": ev.get("id"), "text": text, "at": at, "ms": ms,
                  "type": "final" if ev.get("final") else "draft"})
            if not ev.get("final") or not sid:
                return
            try:
                history_store.append(sid, f"{uuid.uuid4().hex}", at, ms, channel, text)
            except Exception:
                emit({"type": "warning", "error": "写入本地历史失败，请立即复制正文备份"})

        language = (params.get("lang") or self.headers.get("X-Language") or "").strip()
        session = bailian_stream.StreamSession(key, on_event, language)
        holder["session"] = session
        started = time.monotonic()
        if session.fatal:
            alive.clear()
            ws.close()
            return

        emit({"type": "ready", "model": bailian_stream.MODEL, "channel": channel})
        received = 0
        while True:
            try:
                opcode, payload = ws.recv()
            except ws_server.Closed:
                break
            if opcode == ws_server.OP_TEXT:
                try:
                    command = json.loads(payload or b"{}")
                except ValueError:
                    continue
                if command.get("type") == "stop":
                    break
                continue
            if opcode != ws_server.OP_BINARY or not payload:
                continue
            if not session.feed(payload):
                emit({"type": "error", "error": session.fatal or "上行音频中断"})
                break
            received += len(payload)

        session.finish()
        emit({"type": "done", "audio_s": round(received / 32000, 2)})
        session.abort()
        ws.close()
        stats = session.stats()
        print(json.dumps({"event": "stream", "channel": channel,
                          "audio_s": round(received / 32000, 2),
                          "elapsed_ms": round((time.monotonic() - started) * 1000),
                          "upstream_restarts": stats["reconnects"],
                          "dropped_s": stats["dropped_s"],
                          "failure": session.fatal}, ensure_ascii=False), flush=True)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/stream":
            self.stream_asr(self.path)
            return

        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                self._send(200, (HERE / name).read_bytes(), ctype)
            except FileNotFoundError:
                self._send(404, {"error": f"missing {name}"})
            return

        if path == "/api/health":
            self._send(200, {
                # 两路都报可用性，页面自己选；engine/ok/model 保留给旧页面。
                "engines": {"local": lazytyper_ok(), "bailian": bailian_ok()},
                "engine": "bailian-stream" if api_key() else "lazytyper",
                "model": bailian_stream.MODEL if bailian_stream else "",
                "ok": bool(api_key()),
            })
            return

        if path == "/api/history" or path.startswith("/api/history/"):
            try:
                data = history_store.listing() if path == "/api/history" else history_store.detail(path.rsplit("/", 1)[1])
                self._send(200 if data is not None else 404, data if data is not None else {"error": "记录不存在"})
            except Exception:
                self._send(500, {"error": "无法读取本地历史记录"})
            return

        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/history/"):
            self._send(404, {"error": "not found"})
            return
        try:
            history_store.delete(path.rsplit("/", 1)[1])
            self._send(200, {"ok": True})
        except ValueError as exc:
            self._send(404, {"error": str(exc)})
        except Exception:
            self._send(500, {"error": "删除失败，本地数据库无法写入"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/log":
            try:
                raw = self._body()
                event = json.loads(raw or b"{}")
                if not isinstance(event, dict):
                    raise ValueError("需要 JSON 对象")
                event["source"] = "client"
                event["time"] = round(time.time(), 3)
                print(json.dumps(event, ensure_ascii=False)[:2000], flush=True)
                self._send(204, b"")
            except Exception:
                self._send(400, {"error": "日志格式无效"})
            return

        if path == "/api/history" or path.startswith("/api/history/"):
            try:
                self._body()
                if path == "/api/history":
                    self._send(201, history_store.create())
                else:
                    history_store.finish(path.rsplit("/", 1)[1])
                    self._send(200, {"ok": True})
            except Exception as exc:
                self._send(400, {"error": str(exc)})
            return

        if path == "/transcribe":
            try:
                wav = self._body()
            except ValueError as exc:
                self._send(413, {"error": str(exc)})
                return
            if len(wav) < 1000:
                self._send(400, {"error": "音频太短"})
                return
            sid = self.headers.get("X-Session-Id")
            try:
                if sid:
                    if not history_store.exists(sid):
                        raise ValueError("历史记录不存在")
                    segment_id = self.headers.get("X-Segment-Id", "")
                    at = int(self.headers.get("X-Audio-Start", "0"))
                    duration = int(float(self.headers.get("X-Audio-Ms", "0")))
                    channel = self.headers.get("X-Channel", "")
                    if not segment_id or at <= 0 or duration <= 0 or channel not in CHANNELS:
                        raise ValueError("切片元数据无效")
            except Exception as exc:
                self._send(400, {"error": str(exc)})
                return
            started = time.monotonic()
            event = {"event": "transcribe", "time": time.time(), "bytes": len(wav)}
            for key in ("Segment-Id", "Channel", "Audio-Start", "Audio-Ms", "Cut-Reason", "Queue-Ms"):
                event[key] = self.headers.get("X-" + key, "")[:120]
            try:
                result = transcribe(wav, self.headers.get("X-Language") or "")
                text = (result.get("text") or "").strip()
                if sid and text:
                    try:
                        history_store.append(sid, self.headers.get("X-Segment-Id", ""),
                                             int(self.headers.get("X-Audio-Start", "0")),
                                             int(float(self.headers.get("X-Audio-Ms", "0"))),
                                             self.headers.get("X-Channel", ""), text)
                    except Exception:
                        result["history_error"] = "转写成功，但写入本地历史失败；请立即复制正文备份"
                event["status"] = 200
                self._send(200, result)
            except Exception as exc:
                event.update(status=502, error=str(exc))
                self._send(502, {"error": str(exc)})
            finally:
                event["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                print(json.dumps(event, ensure_ascii=False), flush=True)
            return

        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    local = lazytyper_ok()
    print("本地引擎   : LazyTyper 127.0.0.1:%s%s" % (
        local.get("port", "-"), " (ok)" if local["ok"] else " 未就绪 -> %s" % local.get("error")))
    if bailian_stream is None:
        print("百炼引擎   : 不可用 -> %s" % BAILIAN_IMPORT_ERROR)
    else:
        print("百炼引擎   : %s%s" % (
            bailian_stream.MODEL, " (ok)" if api_key() else " 未找到 API Key -> 本地模式仍可用"))

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print("页面       : http://localhost:%d" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()