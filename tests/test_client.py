"""Tests for smtty client behavior."""

import json

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


def test_auto_resume_keeps_saved_mapping(tmp_config, tmp_path, monkeypatch):
    alias = "die0"
    device = tmp_path / "ttyUSB0"
    device.touch()
    info_path = tmp_config.run_dir / f"{alias}.json"
    info_path.write_text(json.dumps({
        "alias": alias,
        "device": str(device),
        "baud": 9600,
        "pid": -1,
        "socket": str(tmp_config.sock_dir / f"{alias}.sock"),
        "ssh": None,
    }))
    commands = []

    class Result:
        returncode = 1
        stderr = "failed"
        stdout = ""

    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or Result(),
    )

    assert client._auto_resume_daemon(tmp_config, alias) is False
    assert info_path.exists()
    assert commands[0][-3:] == [str(device), "--baud", "9600"]


def test_auto_resume_usb_uses_alias_not_saved_device(tmp_config, monkeypatch):
    alias = "die0"
    info_path = tmp_config.run_dir / f"{alias}.json"
    info_path.write_text(json.dumps({
        "alias": alias,
        "device": "/dev/ttyUSB999",
        "usb_port": "pci0000:00/usb/usb-2/usb-2:1.0",
        "usb_instance": "1:4",
        "baud": 115200,
        "pid": -1,
        "socket": str(tmp_config.sock_dir / f"{alias}.sock"),
    }))
    commands = []

    class Result:
        returncode = 1
        stderr = "failed"
        stdout = ""

    monkeypatch.setattr(
        client,
        "_load_alias_info",
        lambda _config, _alias: json.loads(info_path.read_text()),
    )
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or Result(),
    )

    assert client._auto_resume_daemon(tmp_config, alias) is False
    assert commands == [[
        client.sys.executable,
        "-m",
        "serial_mux.cli",
        "start",
        "--alias",
        alias,
    ]]
