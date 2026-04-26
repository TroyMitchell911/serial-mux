"""Tests for serial_mux.protocol — encode/decode, sync/async IO, b64."""

import struct
import pytest

from serial_mux.protocol import (
    encode_msg, decode_msg, b64, unb64,
    sync_read_msg, sync_write_msg,
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
