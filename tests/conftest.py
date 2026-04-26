"""Shared fixtures for serial-mux tests."""

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from serial_mux.config import Config
from serial_mux.protocol import sync_read_msg, sync_write_msg

# Python interpreter that has serial_mux installed — use the same one running tests
import sys as _sys
PYTHON = _sys.executable


@pytest.fixture
def tmp_config(tmp_path):
    """Create a Config pointing to temp directories."""
    cfg = Config()
    cfg.base_dir = tmp_path / "serial-mux"
    cfg.config_dir = tmp_path / "config"
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def socat_pty(tmp_path):
    """Create a virtual serial port pair via socat.

    Returns (pty_device, pty_peer) — the daemon opens pty_device,
    the test writes/reads pty_peer to simulate a real serial device.
    """
    link_a = str(tmp_path / "ptyA")
    link_b = str(tmp_path / "ptyB")
    proc = subprocess.Popen(
        [
            "socat", "-d", "-d",
            f"pty,raw,echo=0,link={link_a}",
            f"pty,raw,echo=0,link={link_b}",
        ],
        stderr=subprocess.PIPE,
    )
    # Wait for PTYs to appear
    for _ in range(50):
        if os.path.exists(link_a) and os.path.exists(link_b):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("socat did not create PTYs in time")

    yield link_a, link_b

    proc.kill()
    proc.wait()


@pytest.fixture
def daemon_foreground(tmp_config, socat_pty):
    """Start a serial-mux daemon in foreground mode as a subprocess.

    Returns (proc, alias, config, pty_peer) — caller can interact with the peer PTY
    and connect to the daemon's Unix socket.
    """
    pty_device, pty_peer = socat_pty
    alias = "test0"

    env = os.environ.copy()
    # Override config dirs via env — the daemon loads Config.load() which reads
    # from default paths, so we run it with --foreground and patch dirs at startup.
    # Instead, we start the daemon directly via Python for better control.
    proc = subprocess.Popen(
        [
            PYTHON, "-c", f"""
import sys, json, asyncio, logging
from pathlib import Path
sys.path.insert(0, '.')
from serial_mux.config import Config
from serial_mux.daemon import SerialDaemon

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')

cfg = Config()
cfg.base_dir = Path("{tmp_config.base_dir}")
cfg.config_dir = Path("{tmp_config.config_dir}")
cfg.ensure_dirs()

daemon = SerialDaemon("{pty_device}", 115200, "{alias}", cfg)
asyncio.run(daemon.run())
""",
        ],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for socket
    sock_path = tmp_config.sock_dir / f"{alias}.sock"
    for _ in range(50):
        if sock_path.exists():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("Daemon did not create socket in time")

    yield proc, alias, tmp_config, pty_peer

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def connect_to_daemon(config, alias):
    """Helper: connect to daemon socket, do handshake, return (sock, transport, history)."""
    sock_path = config.sock_dir / f"{alias}.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(sock_path))
    sock.settimeout(5.0)

    # Hello handshake
    sync_write_msg(sock, {"type": "hello"})
    ack = sync_read_msg(sock)
    assert ack["type"] == "hello_ack"
    transport = ack.get("transport", "serial")

    # Read history
    history = sync_read_msg(sock)
    assert history["type"] == "history"

    return sock, transport, history
