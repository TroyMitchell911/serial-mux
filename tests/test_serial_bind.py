"""Tests for runtime serial-bind / serial-unbind."""

import json
import os
import subprocess
import signal
import time

import pytest

from serial_mux.protocol import sync_write_msg, sync_read_msg, b64, unb64
from tests.conftest import connect_to_daemon, PYTHON


@pytest.mark.timeout(30)
class TestSerialBind:
    """Test binding/unbinding serial ports at runtime."""

    def _start_bare_daemon(self, tmp_config, alias="bindtest"):
        """Start a daemon with no serial and no SSH."""
        proc = subprocess.Popen(
            [
                PYTHON, "-c", f"""
import sys, asyncio, logging
from pathlib import Path
sys.path.insert(0, '.')
from serial_mux.config import Config
from serial_mux.daemon import SerialDaemon

logging.basicConfig(level=logging.DEBUG)
cfg = Config()
cfg.base_dir = Path("{tmp_config.base_dir}")
cfg.config_dir = Path("{tmp_config.config_dir}")
cfg.ensure_dirs()

daemon = SerialDaemon(None, 115200, "{alias}", cfg)
asyncio.run(daemon.run())
""",
            ],
            cwd=str(os.path.dirname(os.path.dirname(__file__))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        sock_path = tmp_config.sock_dir / f"{alias}.sock"
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.1)
        else:
            proc.kill()
            raise RuntimeError("Daemon socket not created")

        return proc

    def test_serial_bind(self, tmp_config, socat_pty):
        """Bind a serial port to a running daemon."""
        pty_device, pty_peer = socat_pty
        alias = "bindtest"
        proc = self._start_bare_daemon(tmp_config, alias)
        try:
            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "serial"  # no transport yet, defaults serial

            # Bind serial
            sync_write_msg(sock, {
                "type": "serial_bind",
                "device": pty_device,
                "baud": 115200,
            })
            resp = sync_read_msg(sock)
            assert resp["type"] == "serial_bind_ack"
            assert resp["ok"] is True

            # Verify info updated
            info = json.loads((tmp_config.run_dir / f"{alias}.json").read_text())
            assert info["device"] == pty_device

            # Verify data flows through
            with open(pty_peer, "wb", buffering=0) as peer:
                peer.write(b"serial-bind-test\r\n")

            collected = b""
            sock.settimeout(3.0)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        collected += unb64(msg["data"])
                        if b"serial-bind-test" in collected:
                            break
                except Exception:
                    break
            assert b"serial-bind-test" in collected
            sock.close()
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)

    def test_serial_unbind(self, tmp_config, socat_pty):
        """Unbind serial from a running daemon."""
        pty_device, pty_peer = socat_pty
        alias = "unbindtest"
        proc = self._start_bare_daemon(tmp_config, alias)
        try:
            sock, _, _ = connect_to_daemon(tmp_config, alias)

            # Bind then unbind
            sync_write_msg(sock, {"type": "serial_bind", "device": pty_device})
            resp = sync_read_msg(sock)
            assert resp["ok"] is True

            sync_write_msg(sock, {"type": "serial_unbind"})
            resp = sync_read_msg(sock)
            assert resp["ok"] is True

            # Verify info updated
            info = json.loads((tmp_config.run_dir / f"{alias}.json").read_text())
            assert info["device"] is None

            sock.close()
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)

    def test_serial_bind_invalid_device(self, tmp_config):
        """Bind to a non-existent device should fail."""
        alias = "badbind"
        proc = self._start_bare_daemon(tmp_config, alias)
        try:
            sock, _, _ = connect_to_daemon(tmp_config, alias)

            sync_write_msg(sock, {
                "type": "serial_bind",
                "device": "/dev/nonexistent_tty_999",
            })
            resp = sync_read_msg(sock)
            assert resp["type"] == "serial_bind_ack"
            assert resp["ok"] is False

            sock.close()
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
