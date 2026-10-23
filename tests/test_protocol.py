"""Tests for serial_mux.protocol — encode/decode, sync/async IO, b64."""

import struct
import pytest

from serial_mux.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    ProtocolError,
    encode_msg, decode_msg, b64, unb64,
    make_envelope, negotiate_capabilities, negotiate_version,
    sync_read_msg, sync_write_msg, validate_envelope,
    HEADER_FMT, HEADER_SIZE, MAX_MSG_SIZE,
)


class TestB64:
    def test_roundtrip_ascii(self):
        data = b"hello world"
        assert unb64(b64(data)) == data

    def test_roundtrip_binary(self):
        data = bytes(range(256))
        assert unb64(b64(data)) == data

    def test_roundtrip_empty(self):
        assert unb64(b64(b"")) == b""


class TestEncodeDecodMsg:
    def test_roundtrip(self):
        msg = {"type": "hello", "data": "test"}
        encoded = encode_msg(msg)
        # Verify header
        length = struct.unpack(HEADER_FMT, encoded[:HEADER_SIZE])[0]
        assert length == len(encoded) - HEADER_SIZE
        # Verify decode
        decoded = decode_msg(encoded[HEADER_SIZE:])
        assert decoded == msg

    def test_unicode(self):
        msg = {"type": "output", "data": "日本語テスト"}
        encoded = encode_msg(msg)
        decoded = decode_msg(encoded[HEADER_SIZE:])
        assert decoded == msg

    def test_nested(self):
        msg = {"type": "history", "lines": ["line1", "line2", "line3"]}
        encoded = encode_msg(msg)
        decoded = decode_msg(encoded[HEADER_SIZE:])
        assert decoded == msg


class TestEnvelope:
    def test_make_and_validate(self):
        message = make_envelope(
            "output",
            {"data": b64(b"hello")},
            request_id="request-1",
            connection_id="connection-1",
        )
        assert message["protocol"] == PROTOCOL_VERSION
        assert validate_envelope(message) == message

    def test_broadcast_cannot_have_request_id(self):
        with pytest.raises(ProtocolError, match="broadcast"):
            validate_envelope(make_envelope("output", broadcast=True, request_id="bad"))

    def test_decode_rejects_invalid_envelope(self):
        with pytest.raises(ProtocolError, match="payload"):
            decode_msg(b'{"protocol":{"major":1,"minor":0},"type":"hello","payload":[]}' )

    def test_negotiate_major_and_minor(self):
        assert negotiate_version({"major": 1, "minor": 99}) == (True, "ok")
        assert negotiate_version({"major": 2, "minor": 0}) == (
            False,
            "protocol_version_incompatible",
        )
        assert negotiate_version({"major": "1", "minor": 0}) == (
            False,
            "protocol_version_invalid",
        )

    def test_negotiate_capabilities_filters_unknown_values(self):
        result = negotiate_capabilities(["request_id", "unknown", 1, "heartbeat"])
        assert result == frozenset({"request_id", "heartbeat"})
        assert result <= CAPABILITIES


class TestSyncSocketIO:
    """Test sync_read_msg / sync_write_msg using a real socketpair."""

    def test_roundtrip(self):
        import socket
        a, b = socket.socketpair()
        try:
            msg = {"type": "hello"}
            sync_write_msg(a, msg)
            result = sync_read_msg(b)
            assert result == msg
        finally:
            a.close()
            b.close()

    def test_multiple_messages(self):
        import socket
        a, b = socket.socketpair()
        try:
            msgs = [
                {"type": "hello"},
                {"type": "hello_ack", "alias": "test", "transport": "serial"},
                {"type": "output", "data": b64(b"test data")},
            ]
            for m in msgs:
                sync_write_msg(a, m)
            for m in msgs:
                result = sync_read_msg(b)
                assert result == m
        finally:
            a.close()
            b.close()

    def test_eof_returns_none(self):
        import socket
        a, b = socket.socketpair()
        a.close()
        result = sync_read_msg(b)
        assert result is None
        b.close()

    def test_large_message(self):
        import socket
        a, b = socket.socketpair()
        try:
            big_data = "x" * 100000
            msg = {"type": "history", "lines": [big_data]}
            sync_write_msg(a, msg)
            result = sync_read_msg(b)
            assert result == msg
        finally:
            a.close()
            b.close()
