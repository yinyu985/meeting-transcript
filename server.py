#!/usr/bin/env python3
"""Meeting transcriber: serves the page and proxies LazyTyper's local ASR API.

    python3 server.py                 # http://localhost:8787

Standard library only. Needs the LazyTyper desktop app running:
POST /v1/audio/transcriptions on 127.0.0.1:<port>.
"""

import argparse
import http.client
import json
import pathlib
import time
import history_store
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
SUPPORT = pathlib.Path.home() / "Library/Application Support/com.lazytyper.desktop"
PORT_FILE = SUPPORT / "server.port"
MAX_BODY = 40 * 1024 * 1024

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/history.js": ("history.js", "text/javascript; charset=utf-8"),
    "/recorder-worklet.js": ("recorder-worklet.js", "text/javascript; charset=utf-8"),
}


# ---------------------------------------------------------------- lazytyper
def upstream_port():
    """Re-read every time: LazyTyper picks a new port on each restart."""
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


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.path.startswith("/transcribe"):
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

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                self._send(200, (HERE / name).read_bytes(), ctype)
            except FileNotFoundError:
                self._send(404, {"error": f"missing {name}"})
            return

        if path == "/api/health":
            self._send(200, {"lazytyper": lazytyper_ok()})
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
                    if not segment_id or at <= 0 or duration <= 0 or channel not in ("mic", "remote"):
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
                if sid:
                    try:
                        history_store.append(sid, segment_id, at, duration, channel, result.get("text", ""))
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

    lt = lazytyper_ok()
    print(
        "LazyTyper  : 127.0.0.1:%s (ok)" % lt["port"]
        if lt["ok"]
        else "LazyTyper  : 未就绪 -> %s" % lt.get("error")
    )

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("页面       : http://localhost:%d" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()