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


@pytest.mark.timeout(30)
class TestGateCommands:
    def test_on_status_off(self, daemon_foreground, monkeypatch, capsys):
        proc, alias, config, pty_peer = daemon_foreground
        monkeypatch.setattr(cli.Config, "load", lambda: config)

        cli.cmd_gate_on(SimpleNamespace(alias=alias, token_stdin=False, note="held by bob"))
        out = capsys.readouterr()
        token = out.out.strip()
        assert len(token) >= 24
        assert "Input gate on" in out.err

        cli.cmd_gate_status(SimpleNamespace(alias=alias))
        assert capsys.readouterr().out.strip() == "on (held by bob)"
        cli.cmd_list(SimpleNamespace())
        assert "gated" in capsys.readouterr().out
        cli.cmd_status(SimpleNamespace(alias=alias))
        assert "Gate:    on (held by bob)" in capsys.readouterr().out

        sock, ack = hello(config, alias, token=token)
        assert ack["input_allowed"] is True
        sock.close()

        cli.cmd_gate_off(SimpleNamespace(alias=alias))
        capsys.readouterr()
        cli.cmd_gate_status(SimpleNamespace(alias=alias))
        assert capsys.readouterr().out.strip() == "off"
        cli.cmd_status(SimpleNamespace(alias=alias))
        assert "Gate:" not in capsys.readouterr().out

    def test_on_reads_the_token_from_stdin(self, daemon_foreground, monkeypatch, capsys):
        proc, alias, config, pty_peer = daemon_foreground
        monkeypatch.setattr(cli.Config, "load", lambda: config)
        monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(readline=lambda: "from-stdin\n"))

        cli.cmd_gate_on(SimpleNamespace(alias=alias, token_stdin=True, note=None))
        assert capsys.readouterr().out == ""
        sock, ack = hello(config, alias, token="from-stdin")
        assert ack["input_allowed"] is True
        sock.close()

    def test_refuses_a_daemon_without_the_gate(self, tmp_config, monkeypatch, capsys):
        """An older daemon would ignore gate_set, so the command must not wait on it."""
        alias = "old0"
        path = tmp_config.sock_dir / f"{alias}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)

        def old_daemon():
            conn, _ = server.accept()
            sync_read_msg(conn)
            sync_write_msg(conn, {"type": "hello_ack", "alias": alias, "device": None, "baud": 115200, "transport": "serial"})
            sync_write_msg(conn, {"type": "history", "lines": []})
            try:
                conn.recv(1)
            except OSError:
                pass
            conn.close()

        thread = threading.Thread(target=old_daemon, daemon=True)
        thread.start()
        (tmp_config.run_dir / f"{alias}.json").write_text(json.dumps({
            "alias": alias, "device": None, "baud": 115200, "pid": os.getpid(), "socket": str(path),
        }))
        monkeypatch.setattr(cli.Config, "load", lambda: tmp_config)

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_gate_on(SimpleNamespace(alias=alias, token_stdin=False, note=None))
        assert exit_info.value.code == 1
        assert "does not support input-gate" in capsys.readouterr().out
        thread.join(timeout=3)
        server.close()


@pytest.mark.timeout(30)
class TestSmttyToken:
    def test_send_without_token_is_refused(self, daemon_foreground, capsys):
        proc, alias, config, pty_peer = daemon_foreground
        control, _ = hello(config, alias)
        request(control, {"type": "gate_set", "token": "secret", "note": "held by carol"})

        with pytest.raises(SystemExit) as exit_info:
            client.noninteractive_mode(config, alias, send_cmd="reboot")
        assert exit_info.value.code == 3
        err = capsys.readouterr().err
        assert "locked" in err and "held by carol" in err
        assert peer_received(pty_peer) == b""
        control.close()

    def test_send_with_token_goes_through(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        control, _ = hello(config, alias)
        request(control, {"type": "gate_set", "token": "secret"})

        client.noninteractive_mode(config, alias, send_cmd="uname", token="secret")
        assert b"uname\r" in peer_received(pty_peer, wait=0)
        control.close()
