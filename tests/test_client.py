"""Tests for smtty client behavior."""

from serial_mux import client
from serial_mux.protocol import b64


class FakeSocket:
    def __init__(self):
        self.blocking = None
        self.closed = False

    def setblocking(self, blocking):
        self.blocking = blocking

    def close(self):
        self.closed = True


def test_noninteractive_sends_once_without_echo(monkeypatch):
    """A response without a command echo must not cause another send."""
    sock = FakeSocket()
    messages = iter([
        {"type": "history", "lines": []},
        {"type": "output", "data": b64(b"READY")},
    ])
    writes = []

    monkeypatch.setattr(client, "connect", lambda config, alias: (sock, "serial"))
    monkeypatch.setattr(client, "sync_read_msg", lambda current_sock: next(messages))
    monkeypatch.setattr(
        client,
        "sync_write_msg",
        lambda current_sock, message: writes.append(message),
    )
    monkeypatch.setattr(client.select, "select", lambda *args: ([sock], [], []))

    client.noninteractive_mode(object(), "port", "reboot", wait_pattern="READY")

    assert writes == [{"type": "input", "data": b64(b"reboot\r")}]
    assert sock.closed is True
