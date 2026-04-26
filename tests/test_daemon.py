"""Tests for daemon lifecycle using socat virtual serial ports."""

import json
import os
import time

import pytest

from serial_mux.protocol import sync_write_msg, sync_read_msg, b64, unb64
from tests.conftest import connect_to_daemon


@pytest.mark.timeout(30)
class TestDaemonLifecycle:
    """Test daemon start, client connect, data flow, and stop."""

    def test_daemon_starts_and_creates_files(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        # Info file
        info_path = config.run_dir / f"{alias}.json"
        assert info_path.exists()
        info = json.loads(info_path.read_text())
        assert info["alias"] == alias
        assert info["baud"] == 115200
        assert info["pid"] == proc.pid

        # PID file
        pid_path = config.run_dir / f"{alias}.pid"
        assert pid_path.exists()
        assert int(pid_path.read_text().strip()) == proc.pid

        # Socket
        sock_path = config.sock_dir / f"{alias}.sock"
        assert sock_path.exists()

    def test_client_connect_handshake(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        sock, transport, history = connect_to_daemon(config, alias)
        assert transport == "serial"
        sock.close()

    def test_send_data_through_serial(self, daemon_foreground):
        """Send data via client -> daemon -> serial, verify on peer side."""
        proc, alias, config, pty_peer = daemon_foreground
        sock, transport, history = connect_to_daemon(config, alias)

        # Send "hello\r" from client
        test_data = b"hello\r"
        sync_write_msg(sock, {"type": "input", "data": b64(test_data)})

        # Read from the peer PTY
        time.sleep(0.3)
        with open(pty_peer, "rb", buffering=0) as peer:
            os.set_blocking(peer.fileno(), False)
            try:
                received = peer.read(4096)
            except BlockingIOError:
                received = b""
        assert test_data in (received or b""), f"Expected {test_data!r} in {received!r}"
        sock.close()

    def test_receive_data_from_serial(self, daemon_foreground):
        """Write data to peer PTY, verify client receives it."""
        proc, alias, config, pty_peer = daemon_foreground
        sock, transport, history = connect_to_daemon(config, alias)
        sock.setblocking(False)

        # Write data to the peer end (simulates device output)
        with open(pty_peer, "wb", buffering=0) as peer:
            peer.write(b"device-output-line\r\n")

        # Read from client
        import select
        time.sleep(0.5)
        collected = b""
        deadline = time.time() + 3.0
        sock.setblocking(True)
        sock.settimeout(3.0)
        while time.time() < deadline:
            try:
                msg = sync_read_msg(sock)
                if msg and msg["type"] == "output":
                    collected += unb64(msg["data"])
                    if b"device-output-line" in collected:
                        break
            except Exception:
                break
        assert b"device-output-line" in collected
        sock.close()

    def test_multiple_clients_receive_broadcast(self, daemon_foreground):
        """Two clients both receive serial data."""
        proc, alias, config, pty_peer = daemon_foreground

        sock1, _, _ = connect_to_daemon(config, alias)
        sock2, _, _ = connect_to_daemon(config, alias)
        sock1.settimeout(3.0)
        sock2.settimeout(3.0)

        # Write data
        with open(pty_peer, "wb", buffering=0) as peer:
            peer.write(b"broadcast-test\r\n")

        time.sleep(0.5)

        for sock in [sock1, sock2]:
            collected = b""
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        collected += unb64(msg["data"])
                        if b"broadcast-test" in collected:
                            break
                except Exception:
                    break
            assert b"broadcast-test" in collected

        sock1.close()
        sock2.close()

    def test_client_count_tracked(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground

        sock1, _, _ = connect_to_daemon(config, alias)
        time.sleep(0.2)
        info = json.loads((config.run_dir / f"{alias}.json").read_text())
        assert info["clients_count"] == 1

        sock2, _, _ = connect_to_daemon(config, alias)
        time.sleep(0.2)
        info = json.loads((config.run_dir / f"{alias}.json").read_text())
        assert info["clients_count"] == 2

        sock1.close()
        time.sleep(0.5)
        info = json.loads((config.run_dir / f"{alias}.json").read_text())
        assert info["clients_count"] == 1

        sock2.close()


@pytest.mark.timeout(30)
class TestDaemonLogging:
    def test_log_file_created(self, daemon_foreground):
        proc, alias, config, pty_peer = daemon_foreground
        sock, _, _ = connect_to_daemon(config, alias)

        # Generate some output
        with open(pty_peer, "wb", buffering=0) as peer:
            peer.write(b"log-test-line\r\n")
        time.sleep(1.0)

        log_dir = config.logs_dir / alias
        assert log_dir.exists()
        logs = list(log_dir.glob("*.log"))
        assert len(logs) >= 1
        content = logs[0].read_text()
        assert "log-test-line" in content
        sock.close()
