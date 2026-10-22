"""Protocol definitions for daemon <-> client communication over Unix socket.

Message format: 4-byte big-endian length prefix + JSON payload.

Message types:

Client -> Daemon:
  {"type": "hello", "interactive": true|false}
  {"type": "input", "data": "<base64 encoded bytes>"}
  {"type": "history_request"}
  {"type": "set_baud", "baud": <int>}
  {"type": "ssh_bind", "target": "<user@host or ssh-config-host>"}
  {"type": "ssh_unbind"}
  {"type": "serial_bind", "device": "<path>", "baud": <int>}
  {"type": "serial_unbind"}

Daemon -> Client:
  {"type": "hello_ack", "alias": "...", "device": "...", "baud": ...,
   "transport": "ssh"|"serial", "terminal_owner": true|false}
  {"type": "output", "data": "<base64 encoded bytes>"}
  {"type": "history", "lines": ["...", ...]}
  {"type": "error", "message": "..."}
  {"type": "baud_ack", "baud": <int>}
  {"type": "ssh_bind_ack", "target": "...", "ok": true/false, "message": "..."}
  {"type": "serial_bind_ack", "device": "...", "ok": true/false, "message": "..."}
  {"type": "transport_changed", "transport": "ssh"|"serial"}
  {"type": "terminal_owner", "active": true|false}
Agent protocol envelope v1:
  {
    "protocol": {"major": 1, "minor": 0},
    "type": "output",
    "request_id": "optional-request-id",
    "connection_id": "connection-epoch",
    "broadcast": false,
    "payload": {}
  }

Major protocol versions must match. Minor-compatible peers ignore unknown
fields. Unknown message types must not mutate device state. Binary payloads
continue to use base64. Broadcast messages do not carry request IDs.
"""

import base64
import json
import struct
from typing import Optional


HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAX_MSG_SIZE = 1024 * 1024  # 1MB

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PROTOCOL_VERSION = {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}

CAPABILITIES = frozenset({
    "request_id",
    "output_replay",
    "input_status",
    "heartbeat",
    "pairing",
})


class ProtocolError(ValueError):
    """Raised when a protocol message has an invalid structure."""


def make_envelope(
    msg_type: str,
    payload: Optional[dict] = None,
    *,
    request_id: Optional[str] = None,
    connection_id: Optional[str] = None,
    broadcast: bool = False,
) -> dict:
    """Build a versioned Agent message without mutating its payload."""
    if not isinstance(msg_type, str) or not msg_type:
        raise ProtocolError("message type must be a non-empty string")
    if payload is not None and not isinstance(payload, dict):
        raise ProtocolError("message payload must be an object")

    message = {
        "protocol": dict(PROTOCOL_VERSION),
        "type": msg_type,
        "payload": dict(payload or {}),
    }
    if request_id is not None:
        message["request_id"] = request_id
    if connection_id is not None:
        message["connection_id"] = connection_id
    if broadcast:
        message["broadcast"] = True
    return message


def validate_envelope(message: dict) -> dict:
    """Validate and return a versioned Agent message."""
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")

    version = message.get("protocol")
    if not isinstance(version, dict):
        raise ProtocolError("protocol must be an object")
    for field in ("major", "minor"):
        value = version.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProtocolError(f"protocol.{field} must be a non-negative integer")

    msg_type = message.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise ProtocolError("type must be a non-empty string")

    payload = message.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")

    for field in ("request_id", "connection_id"):
        value = message.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ProtocolError(f"{field} must be a non-empty string")

    broadcast = message.get("broadcast", False)
    if not isinstance(broadcast, bool):
        raise ProtocolError("broadcast must be a boolean")
    if broadcast and message.get("request_id") is not None:
        raise ProtocolError("broadcast messages cannot have request_id")

    return message


def is_envelope(message: object) -> bool:
    """Return whether a message has the versioned Agent envelope shape."""
    return isinstance(message, dict) and "protocol" in message


def negotiate_version(remote_version: dict) -> tuple[bool, str]:
    """Check whether a remote version is compatible with this implementation."""
    if not isinstance(remote_version, dict):
        return False, "protocol_version_invalid"
    major = remote_version.get("major")
    minor = remote_version.get("minor")
    if (
        not isinstance(major, int)
        or isinstance(major, bool)
        or major < 0
        or not isinstance(minor, int)
        or isinstance(minor, bool)
        or minor < 0
    ):
        return False, "protocol_version_invalid"
    if major != PROTOCOL_MAJOR:
        return False, "protocol_version_incompatible"
    return True, "ok"


def negotiate_capabilities(remote: object) -> frozenset[str]:
    """Return the capabilities supported by both peers."""
    if not isinstance(remote, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        capability
        for capability in remote
        if isinstance(capability, str) and capability in CAPABILITIES
    )


def encode_msg(msg: dict) -> bytes:
    """Encode a message dict to wire format (length-prefixed JSON)."""
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return struct.pack(HEADER_FMT, len(payload)) + payload


def decode_msg(data: bytes) -> dict:
    """Decode a JSON payload to message dict."""
    message = json.loads(data.decode("utf-8"))
    if is_envelope(message):
        return validate_envelope(message)
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    msg_type = message.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise ProtocolError("type must be a non-empty string")
    return message


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
