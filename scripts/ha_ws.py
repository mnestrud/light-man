#!/usr/bin/env python3
"""Minimal stdlib Home Assistant websocket client (no third-party deps).

Usage:
    python3 scripts/ha_ws.py '{"type":"system_log/list"}' ['{"type":"..."}' ...]

Prints one JSON result per command (or {"error": ...}). Auth token from ~/.ha-token
(override with HA_TOKEN_FILE); host from HA_WS_HOST (default meddle.home.botworth.net).

Useful commands: system_log/list, repairs/list_issues, config_entries/get,
config/entity_registry/list, light_man/config, and the Supervisor proxy
{"type":"supervisor/api","endpoint":"/addons","method":"get"}.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import sys
from pathlib import Path
from typing import Any

HOST = os.environ.get("HA_WS_HOST", "meddle.home.botworth.net")
TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", "~/.ha-token")).expanduser()


class WsClient:
    """A single authenticated HA websocket connection."""

    def __init__(self, host: str) -> None:
        """Open the TLS socket, complete the HTTP upgrade, and authenticate."""
        ctx = ssl.create_default_context()
        raw = socket.create_connection((host, 443), timeout=60)
        self._sock = ctx.wrap_socket(raw, server_hostname=host)
        self._buf = b""
        self._upgrade(host)
        if self.recv()["type"] != "auth_required":
            raise SystemExit("unexpected handshake")
        self.send({"type": "auth", "access_token": TOKEN_FILE.read_text().strip()})
        reply = self.recv()
        if reply["type"] != "auth_ok":
            raise SystemExit(f"auth failed: {reply}")

    def _upgrade(self, host: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /api/websocket HTTP/1.1\r\nHost: {host}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.send(request.encode())
        while b"\r\n\r\n" not in self._buf:
            self._buf += self._sock.recv(4096)
        self._buf = self._buf.split(b"\r\n\r\n", 1)[1]

    def _exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._sock.recv(65536)
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def recv(self) -> dict[str, Any]:
        """Return the next text frame as JSON (skips non-text frames)."""
        while True:
            header = self._exact(2)
            opcode, length = header[0] & 0xF, header[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(length)
            if opcode == 1:
                return json.loads(payload)  # type: ignore[no-any-return]
            if opcode == 8:
                raise SystemExit("connection closed by server")

    def send(self, obj: dict[str, Any]) -> None:
        """Send one masked text frame."""
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        if len(payload) < 126:
            header = bytes([0x81, 0x80 | len(payload)])
        elif len(payload) < 65536:
            header = bytes([0x81, 0xFE]) + struct.pack(">H", len(payload))
        else:
            header = bytes([0x81, 0xFF]) + struct.pack(">Q", len(payload))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.send(header + mask + masked)

    def call(self, msg_id: int, command: dict[str, Any]) -> dict[str, Any]:
        """Send a numbered command and wait for its result envelope."""
        self.send({**command, "id": msg_id})
        while True:
            reply = self.recv()
            if reply.get("id") == msg_id and reply["type"] == "result":
                return reply


def main(argv: list[str]) -> None:
    """Run each JSON command argument in order and print its result."""
    client = WsClient(HOST)
    for msg_id, arg in enumerate(argv, 1):
        reply = client.call(msg_id, json.loads(arg))
        out = reply.get("result") if reply["success"] else {"error": reply["error"]}
        print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])
