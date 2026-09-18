"""Tests for smtty client behavior."""

import json
import os
import pty
import select
import termios
import tty

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


class FakeTTY:
    def __init__(self, fd):
        self.fd = fd
        self.output = []

    def fileno(self):
        return self.fd

    def write(self, value):
        self.output.append(value)
        return len(value)

    def flush(self):
        pass


def test_history_sanitizer_drops_terminal_state_without_eating_text():
    line = (
        "\x1b(0abc\x1b(B "
        "\x1b[1;24rbody "
        "\x1b]0;title\x07tail"
    )

    assert client._sanitize_history_line(line) == "abc body tail"
    assert client._sanitize_history_line("\x1b(0") == ""


def test_history_sanitizer_preserves_sgr_only():
    line = "\x1b[31mred\x1b[0m\r\x08"

    assert client._sanitize_history_line(line) == "\x1b[31mred\x1b[0m"


def test_terminal_query_filter_handles_split_sequences():
    query_filter = client._TerminalQueryFilter()

    assert query_filter.feed(b"before\x1b[") == b"before"
    assert query_filter.feed(b"6nafter") == b"after"
    assert query_filter.feed(b"\x1b[18t") == b""
    assert query_filter.feed(b"\x1b[31mred") == b"\x1b[31mred"


def test_restore_local_terminal_resets_pty_state():
    master_fd, slave_fd = pty.openpty()
    saved_settings = termios.tcgetattr(slave_fd)

    try:
        tty.setraw(slave_fd)
        client._restore_local_terminal(
            slave_fd,
            slave_fd,
            saved_settings,
        )

        readable, _, _ = select.select([master_fd], [], [], 1.0)
        assert readable == [master_fd]
        assert os.read(master_fd, 4096) == client._TERMINAL_CLEANUP
        assert termios.tcgetattr(slave_fd) == saved_settings
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_interactive_mode_claims_terminal_and_restores_on_detach(
    monkeypatch,
):
    sock = FakeSocket()
    fake_stdin = FakeTTY(10)
    fake_stdout = FakeTTY(11)
    terminal_writes = []
    restored = []
    connections = []

    def fake_connect(config, alias, interactive=False):
        connections.append((alias, interactive))
        return sock, "serial"

    monkeypatch.setattr(client, "connect", fake_connect)
    monkeypatch.setattr(
        client,
        "sync_read_msg",
        lambda current_sock: {"type": "history", "lines": []},
    )
    monkeypatch.setattr(client.sys, "stdin", fake_stdin)
    monkeypatch.setattr(client.sys, "stdout", fake_stdout)
    monkeypatch.setattr(client.os, "isatty", lambda fd: True)
    monkeypatch.setattr(
        client.os,
        "write",
        lambda fd, data: terminal_writes.append(bytes(data)) or len(data),
    )
    monkeypatch.setattr(client.os, "read", lambda fd, size: b"\x1d")
    monkeypatch.setattr(
        client.select,
        "select",
        lambda *args: ([fake_stdin], [], []),
    )
    monkeypatch.setattr(client.termios, "tcgetattr", lambda fd: "saved")
    monkeypatch.setattr(
        client.termios,
        "tcsetattr",
        lambda fd, when, value: restored.append((fd, when, value)),
    )
    monkeypatch.setattr(client.tty, "setraw", lambda fd: None)

    client.interactive_mode(object(), "com260")

    assert connections == [("com260", True)]
    assert terminal_writes[0] == client._TERMINAL_NORMALIZE
    assert terminal_writes[-1] == client._TERMINAL_CLEANUP
    assert restored == [(10, termios.TCSADRAIN, "saved")]
    assert sock.closed is True
    assert "--- detached ---" in "".join(fake_stdout.output)


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
