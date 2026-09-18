"""Render client output to catch cursor and scrolling regressions."""

import pyte
import pytest

from serial_mux import client
from serial_mux.protocol import b64


class Terminal:
    def __init__(self, rows):
        self.screen = pyte.Screen(100, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.frames = []

    def fileno(self):
        return 11

    def write(self, text):
        self.feed(text.encode())
        return len(text)

    def flush(self):
        pass

    def feed(self, data):
        self.stream.feed(bytes(data))
        self.frames.append(self.screen.display[:])
        return len(data)


class Socket:
    def setblocking(self, value):
        pass

    def settimeout(self, value):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("rows", [24, 44])
@pytest.mark.parametrize("history_count", [0, 3, 80])
@pytest.mark.parametrize("disconnect", [False, True])
def test_attach_appends_live_output_after_history(
    monkeypatch, rows, history_count, disconnect,
):
    terminal = Terminal(rows)
    sock = Socket()
    history = [f"history-{index}" for index in range(history_count)]
    # Simulate state left behind by a TUI, including origin mode and margins.
    terminal.feed(b"\x1b[2;12r\x1b[?6h\x1b(0")
    messages = iter([
        {"type": "history", "lines": history},
        {"type": "output", "data": b64(b"live-first\r\n")},
        {"type": "output", "data": b64(b"live-second\r\n")},
        None,
    ])
    readable = iter([[sock], [sock], [sock if disconnect else terminal]])
    monkeypatch.setattr(
        client, "connect", lambda *args, **kwargs: (sock, "serial"),
    )
    monkeypatch.setattr(client, "sync_read_msg", lambda sock: next(messages))
    monkeypatch.setattr(client.sys, "stdin", terminal)
    monkeypatch.setattr(client.sys, "stdout", terminal)
    monkeypatch.setattr(client.os, "isatty", lambda fd: True)
    monkeypatch.setattr(
        client.os, "write", lambda fd, data: terminal.feed(data),
    )
    monkeypatch.setattr(client.os, "read", lambda fd, count: b"\x1d")
    monkeypatch.setattr(client.termios, "tcgetattr", lambda fd: [])
    monkeypatch.setattr(client.termios, "tcsetattr", lambda *args: None)
    monkeypatch.setattr(client.tty, "setraw", lambda fd: None)
    monkeypatch.setattr(
        client.select, "select", lambda *args: (next(readable), [], []),
    )

    client.interactive_mode(object(), "com260")

    # Inspect the screen at the moment the second live line is rendered.
    # Checking emitted escape bytes alone misses their cursor side effects.
    frame = next(
        frame for frame in terminal.frames
        if any(line.strip() == "live-second" for line in frame)
    )
    lines = [line.strip() for line in frame if line.strip()]
    banner_index = next(
        index for index, line in enumerate(lines)
        if "serial-mux: attached" in line
    )
    assert lines[banner_index + 1:] == ["live-first", "live-second"]
    if history:
        assert lines[banner_index - 1] == history[-1]
    # Live output must scroll at the bottom, not paint over the top rows.
    assert frame[-2].strip() == "live-second"

    terminal.feed(b"local-shell$ ")
    final_lines = [line.strip() for line in terminal.screen.display]
    assert final_lines[-1] == "local-shell$"
    assert "live-second" in final_lines[:-1]


@pytest.mark.parametrize("rows", [24, 44])
def test_cleanup_restores_full_screen_scrolling(monkeypatch, rows):
    terminal = Terminal(rows)
    terminal.feed(b"\x1b[2;12r\x1b[?6h\x1b(0")
    monkeypatch.setattr(client.os, "isatty", lambda fd: True)
    monkeypatch.setattr(
        client.os, "write", lambda fd, data: terminal.feed(data),
    )
    monkeypatch.setattr(client.termios, "tcsetattr", lambda *args: None)

    client._restore_local_terminal(10, 11, [])
    for index in range(rows + 2):
        terminal.feed(f"line-{index}\r\n".encode())

    assert terminal.screen.display[0].strip() == "line-3"
    assert terminal.screen.display[-2].strip() == f"line-{rows + 1}"
    assert terminal.screen.cursor.y == rows - 1


def test_sanitized_history_does_not_generate_terminal_replies():
    terminal = Terminal(44)
    replies = []
    terminal.screen.write_process_input = replies.append
    history = "\x1b[6n\x1b[32766;32766H\x1b[6n\x1b[1;1Hboot"

    terminal.feed(client._sanitize_history_line(history).encode())

    assert replies == []
    assert terminal.screen.display[0].strip() == "boot"
