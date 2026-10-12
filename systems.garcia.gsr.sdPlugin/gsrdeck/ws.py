"""A minimal RFC 6455 client, enough to talk to OpenDeck.

OpenDeck speaks the Elgato Stream Deck plugin protocol over a local WebSocket.
The full frame spec is not needed: the host is on loopback, sends only text
frames, and never fragments. What IS needed is masking every client frame,
answering pings, and handling a close cleanly -- skipping any of those makes
the host drop the connection minutes later for no visible reason.

Written against the standard library so the plugin has no install step.
"""

import base64
import json
import os
import socket
import struct

_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocket:
    def __init__(self, port, host="127.0.0.1", timeout=None):
        self._sock = socket.create_connection((host, port))
        self._sock.settimeout(timeout)
        self._buf = b""
        self._handshake(host, port)

    def _handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("host closed during handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"handshake refused: {head.splitlines()[0]!r}")
        self._buf = rest

    # ---------------------------------------------------------------- send
    def send_json(self, payload):
        self._send(_OP_TEXT, json.dumps(payload).encode())

    def _send(self, opcode, data):
        header = bytearray([0x80 | opcode])
        length = len(data)
        # Every client frame must be masked; an unmasked one is a protocol
        # violation the host is entitled to close on.
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes(header) + masked)

    # ---------------------------------------------------------------- recv
    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("host closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def receive(self):
        """Return the next text message, or None for a frame we handled.

        Returns None rather than blocking on control frames so the caller's
        loop stays responsive; raises ConnectionError when the host goes away.
        """
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if second & 0x80 else None
        data = self._recv_exact(length) if length else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        if opcode == _OP_TEXT:
            return data.decode("utf-8", "replace")
        if opcode == _OP_PING:
            self._send(_OP_PONG, data)
            return None
        if opcode == _OP_CLOSE:
            raise ConnectionError("host sent close")
        return None

    def pending(self):
        """Whether a message is already buffered, waiting to be read.

        select() reports the socket, not this buffer. A host that sends six
        frames in one segment -- which is exactly what happens when a profile
        loads and every key appears at once -- wakes the loop once, and the
        five frames after the first would sit here unread until some later
        message happened to arrive. The caller drains on this instead.
        """
        return bool(self._buf)

    def fileno(self):
        """So the caller can select() on us alongside its other inputs."""
        return self._sock.fileno()

    def close(self):
        try:
            self._send(_OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
