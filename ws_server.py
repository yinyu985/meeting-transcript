"""Minimal RFC 6455 server endpoint on top of BaseHTTPRequestHandler.

Only what this project needs: handshake, binary/text frames, ping/pong, close.
No extensions, no compression, no fragmentation on the outgoing side.
"""

import base64
import hashlib
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class Closed(Exception):
    pass


def accept(handler):
    """Complete the upgrade; raises ValueError when the request is not a WS handshake."""
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or "websocket" not in (handler.headers.get("Upgrade") or "").lower():
        raise ValueError("缺少 WebSocket 握手头")
    digest = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    handler.wfile.write(
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {digest}\r\n\r\n".encode()
    )
    handler.wfile.flush()
    handler.close_connection = True
    return Connection(handler)


class Connection:
    def __init__(self, handler):
        self.rfile = handler.rfile
        self.wfile = handler.wfile

    # ---------------------------------------------------------------- receive
    def _read(self, n):
        data = self.rfile.read(n)
        if not data or len(data) < n:
            raise Closed()
        return data

    def recv(self):
        """Return (opcode, payload). Handles ping/pong/close internally."""
        fragment = b""
        fragment_op = None
        while True:
            b0, b1 = self._read(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length > 4 * 1024 * 1024:
                raise Closed()
            mask = self._read(4) if masked else b"\0\0\0\0"
            payload = bytearray(self._read(length)) if length else bytearray()
            if masked:
                for i in range(length):
                    payload[i] ^= mask[i & 3]
            payload = bytes(payload)

            if opcode == OP_CLOSE:
                raise Closed()
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONT:
                fragment += payload
                if fin:
                    return fragment_op, fragment
                continue
            if fin:
                return opcode, payload
            fragment_op, fragment = opcode, payload

    # ------------------------------------------------------------------- send
    def _send(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        self.wfile.write(bytes(header) + payload)
        self.wfile.flush()

    def send_text(self, text):
        self._send(OP_TEXT, text.encode())

    def close(self, code=1000):
        try:
            self._send(OP_CLOSE, struct.pack("!H", code))
        except Exception:
            pass