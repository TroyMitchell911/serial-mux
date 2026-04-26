"""Protocol definitions for daemon <-> client communication over Unix socket.

Message format: 4-byte big-endian length prefix + JSON payload.

Message types:

Client -> Daemon:
  {"type": "hello"}
  {"type": "input", "data": "<base64 encoded bytes>"}
  {"type": "history_request"}
  {"type": "set_baud", "baud": <int>}
  {"type": "ssh_bind", "target": "<user@host or ssh-config-host>"}
  {"type": "ssh_unbind"}

Daemon -> Client:
  {"type": "hello_ack", "alias": "...", "device": "...", "baud": ..., "transport": "ssh"|"serial"}
  {"type": "output", "data": "<base64 encoded bytes>"}
  {"type": "history", "lines": ["...", ...]}
  {"type": "error", "message": "..."}
  {"type": "baud_ack", "baud": <int>}
  {"type": "ssh_bind_ack", "target": "...", "ok": true/false, "message": "..."}
  {"type": "transport_changed", "transport": "ssh"|"serial"}
"""

import base64
import json
import struct
from typing import Optional


HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAX_MSG_SIZE = 1024 * 1024  # 1MB


def encode_msg(msg: dict) -> bytes:
    """Encode a message dict to wire format (length-prefixed JSON)."""
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return struct.pack(HEADER_FMT, len(payload)) + payload


def decode_msg(data: bytes) -> dict:
    """Decode a JSON payload to message dict."""
    return json.loads(data.decode("utf-8"))


async def async_read_msg(reader) -> Optional[dict]:
    """Read one message from an asyncio StreamReader."""
    header = await reader.readexactly(HEADER_SIZE)
    (length,) = struct.unpack(HEADER_FMT, header)
    if length > MAX_MSG_SIZE:
        raise ValueError(f"Message too large: {length}")
    payload = await reader.readexactly(length)
    return decode_msg(payload)


async def async_write_msg(writer, msg: dict):
    """Write one message to an asyncio StreamWriter."""
    writer.write(encode_msg(msg))
    await writer.drain()


def sync_read_msg(sock) -> Optional[dict]:
    """Read one message from a blocking socket."""
    header = _recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None
    (length,) = struct.unpack(HEADER_FMT, header)
    if length > MAX_MSG_SIZE:
        raise ValueError(f"Message too large: {length}")
    payload = _recv_exact(sock, length)
    if payload is None:
        return None
    return decode_msg(payload)


def sync_write_msg(sock, msg: dict):
    """Write one message to a blocking socket."""
    sock.sendall(encode_msg(msg))


def _recv_exact(sock, n: int) -> Optional[bytes]:
    """Receive exactly n bytes from a socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def b64(data: bytes) -> str:
    """Encode bytes to base64 string."""
    return base64.b64encode(data).decode("ascii")


def unb64(s: str) -> bytes:
    """Decode base64 string to bytes."""
    return base64.b64decode(s)
