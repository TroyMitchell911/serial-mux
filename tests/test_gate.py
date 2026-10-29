"""Tests for the optional input gate: a token guards input, output stays open."""

import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from serial_mux import cli, client
from serial_mux.protocol import b64, sync_read_msg, sync_write_msg, unb64


def hello(config, alias, token=None):
    """Connect with an optional gate token; return (sock, hello_ack)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(config.sock_dir / f"{alias}.sock"))
    sock.settimeout(3.0)
    msg = {"type": "hello", "interactive": False}
    if token:
        msg["token"] = token
    sync_write_msg(sock, msg)
    ack = sync_read_msg(sock)
    assert ack["type"] == "hello_ack"
    assert sync_read_msg(sock)["type"] == "history"
    return sock, ack


def request(sock, msg):
    """Send a control message and return the first reply that is not output."""
    sync_write_msg(sock, msg)
    while True:
        reply = sync_read_msg(sock)
        if reply["type"] != "output":
            return reply


def next_message(sock, wait=0.5):
    """The next message other than output, or None if none arrives in time."""
    sock.settimeout(wait)
    try:
        while True:
            msg = sync_read_msg(sock)
            if msg["type"] != "output":
                return msg
    except (socket.timeout, TimeoutError):
        return None
    finally:
        sock.settimeout(3.0)


def type_line(sock, text):
    sync_write_msg(sock, {"type": "input", "data": b64(text)})


def peer_received(pty_peer, wait=0.3):
    time.sleep(wait)
    with open(pty_peer, "rb", buffering=0) as peer:
        os.set_blocking(peer.fileno(), False)
        try:
            return peer.read(4096) or b""
        except BlockingIOError:
            return b""


def output_seen(sock, marker):
    deadline = time.time() + 3.0
    collected = b""
    while time.time() < deadline and marker not in collected:
        msg = sync_read_msg(sock)
        if msg["type"] == "output":
            collected += unb64(msg["data"])
    return marker in collected


def info(config, alias):
    return json.loads((config.run_dir / f"{alias}.json").read_text())


@pytest.mark.timeout(30)
class TestInputGate:
    def test_off_by_default_changes_nothing(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        sock, ack = hello(config, alias)
        assert "input-gate" in ack["capabilities"]
        assert ack["input_gate"] is False
        assert ack["input_allowed"] is True
        assert "input_gate" not in info(config, alias)

        type_line(sock, b"free\r")
        assert b"free\r" in peer_received(pty_peer)
        assert next_message(sock) is None
        sock.close()

    def test_refuses_input_without_token_but_keeps_output(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        holder, _ = hello(config, alias, token="t1")
        assert request(holder, {"type": "gate_set", "token": "t1", "note": "held by alice"}) == {
            "type": "gate_ack", "enabled": True, "note": "held by alice",
        }
        assert info(config, alias)["input_gate"] is True

        other, ack = hello(config, alias)
        assert ack["input_gate"] is True
        assert ack["input_allowed"] is False
        assert ack["gate_note"] == "held by alice"

        type_line(other, b"intruder\r")
        rejected = next_message(other)
        assert rejected["type"] == "input_rejected"
        assert "locked" in rejected["message"]
        assert rejected["note"] == "held by alice"
        # One notice per gate change, not one per keystroke.
        type_line(other, b"again\r")
        assert next_message(other) is None
        assert peer_received(pty_peer) == b""

        with open(pty_peer, "wb", buffering=0) as peer:
            peer.write(b"still-visible\r\n")
        assert output_seen(other, b"still-visible")
        assert output_seen(holder, b"still-visible")

        type_line(holder, b"owner\r")
        assert b"owner\r" in peer_received(pty_peer)
        holder.close()
        other.close()

    def test_new_token_replaces_the_old_one(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        control, _ = hello(config, alias)
        request(control, {"type": "gate_set", "token": "first"})
        old, ack = hello(config, alias, token="first")
        assert ack["input_allowed"] is True

        assert request(control, {"type": "gate_set", "token": "second"})["enabled"] is True
        type_line(old, b"stale\r")
        assert next_message(old)["type"] == "input_rejected"
        assert peer_received(pty_peer) == b""

        new, ack = hello(config, alias, token="second")
        assert ack["input_allowed"] is True
        type_line(new, b"fresh\r")
        assert b"fresh\r" in peer_received(pty_peer)
        for sock in (control, old, new):
            sock.close()

    def test_turning_it_off_lets_everyone_write(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        sock, _ = hello(config, alias)
        request(sock, {"type": "gate_set", "token": "t"})
        type_line(sock, b"blocked\r")
        assert next_message(sock)["type"] == "input_rejected"

        assert request(sock, {"type": "gate_clear"}) == {"type": "gate_ack", "enabled": False, "note": None}
        assert "input_gate" not in info(config, alias)
        type_line(sock, b"open\r")
        assert b"open\r" in peer_received(pty_peer)
        assert request(sock, {"type": "gate_status"})["enabled"] is False
        sock.close()

    def test_rejects_an_empty_token(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        sock, _ = hello(config, alias)
        reply = request(sock, {"type": "gate_set", "token": "  "})
        assert reply["type"] == "error"
        assert request(sock, {"type": "gate_status"})["enabled"] is False
        sock.close()
