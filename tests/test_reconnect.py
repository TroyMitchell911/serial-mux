"""Tests for automatic reconnect: daemon serial re-bind and client reconnect."""

import asyncio

import pytest
import serial

from serial_mux import client
from serial_mux.config import Config
from serial_mux.daemon import SerialDaemon


class FakeSerial:
    """Minimal stand-in for pyserial's serial.Serial."""

    is_open = True

    def __init__(self, *args, **kwargs):
        pass

    def read(self, n):
        return b""

    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def _make_daemon(tmp_path):
    cfg = Config()
    cfg.base_dir = tmp_path / "serial-mux"
    cfg.config_dir = tmp_path / "config"
    cfg.ensure_dirs()
    cfg.serial_reconnect_interval = 0.01
    daemon = SerialDaemon("/dev/ttyUSB0", 115200, "recon", cfg)
    return daemon, cfg


async def _collect(broadcasts, msg):
    broadcasts.append(msg)


# --- daemon: _drop_serial ---------------------------------------------------


def test_drop_serial_preserves_usb_port_and_broadcasts(tmp_path, monkeypatch):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon.ser = FakeSerial()
    daemon._usb_info = {"usb_port": "p0", "usb_instance": "1:4"}
    daemon._usb_port = "p0"
    broadcasts = []

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(daemon, "_broadcast", fake_broadcast)

    asyncio.run(daemon._drop_serial("boom"))

    assert daemon.ser is None
    assert daemon.device is None
    assert daemon._usb_info == {}
    # The physical port identity must survive so the daemon can rediscover it.
    assert daemon._usb_port == "p0"
    assert any(m["type"] == "serial_lost" for m in broadcasts)


# --- daemon: _wait_for_serial -------------------------------------------------


def test_wait_for_serial_without_any_device_gives_up(tmp_path):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon._usb_port = None
    daemon._last_device_path = None
    daemon.running = True
    assert asyncio.run(daemon._wait_for_serial()) is False


def test_wait_for_serial_rebinds_when_device_reappears(tmp_path, monkeypatch):
    daemon, cfg = _make_daemon(tmp_path)
    daemon._usb_port = "p0"
    daemon.running = True
    lookups = {"count": 0}
    broadcasts = []

    def fake_find(port):
        lookups["count"] += 1
        if lookups["count"] >= 3:
            return "/dev/ttyUSB9", {"usb_port": port, "usb_instance": "1:9"}
        return None, {}

    def fake_open_serial():
        daemon.ser = FakeSerial()

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr("serial_mux.daemon.find_usb_device", fake_find)
    monkeypatch.setattr(daemon, "_open_serial", fake_open_serial)
    monkeypatch.setattr(daemon, "_broadcast", fake_broadcast)

    result = asyncio.run(daemon._wait_for_serial())

    assert result is True
    assert lookups["count"] >= 3
    assert daemon.device == "/dev/ttyUSB9"
    assert daemon._usb_info == {"usb_port": "p0", "usb_instance": "1:9"}
    assert daemon.ser is not None
    assert any(m["type"] == "serial_restored" for m in broadcasts)


def test_wait_for_serial_polls_device_node_fallback(tmp_path, monkeypatch):
    """Without a USB port identity, keep polling the remembered device node."""
    daemon, _cfg = _make_daemon(tmp_path)
    daemon._usb_port = None
    node = tmp_path / "ttyUSB7"
    node.touch()
    daemon._last_device_path = str(node)
    daemon.running = True
    broadcasts = []

    def fake_open_serial():
        daemon.ser = FakeSerial()

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(daemon, "_open_serial", fake_open_serial)
    monkeypatch.setattr(daemon, "_broadcast", fake_broadcast)

    result = asyncio.run(daemon._wait_for_serial())

    assert result is True
    assert daemon.device == str(node)
    assert daemon.ser is not None
    assert any(m["type"] == "serial_restored" for m in broadcasts)


# --- daemon: _serial_reader recovery loop ------------------------------------


def test_serial_reader_recovers_after_serial_loss(tmp_path, monkeypatch):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon.running = True
    daemon.ser = FakeSerial()
    daemon._usb_port = "p0"
    broadcasts = []
    state = {"reads": 0}

    def fake_serial_read():
        state["reads"] += 1
        if state["reads"] == 1:
            raise serial.SerialException("device vanished")
        if state["reads"] >= 4:
            daemon.running = False
        return b"hello\n"

    async def fake_wait_for_serial():
        daemon.ser = FakeSerial()
        return True

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(daemon, "_serial_read", fake_serial_read)
    monkeypatch.setattr(daemon, "_wait_for_serial", fake_wait_for_serial)
    monkeypatch.setattr(daemon, "_broadcast", fake_broadcast)
    monkeypatch.setattr(daemon, "_log_write", lambda line: None)

    asyncio.run(daemon._serial_reader())

    types = [m["type"] for m in broadcasts]
    assert "serial_lost" in types
    assert types.count("output") >= 2
    # The reader did not die on the first error: it kept reading after rebind.
    assert state["reads"] >= 4


# --- client: ConnectError and reconnect policy -------------------------------


def test_connect_raises_non_retryable_when_no_daemon(tmp_config, monkeypatch):
    monkeypatch.setattr(client, "resolve_socket", lambda cfg, alias: "")
    monkeypatch.setattr(client, "_is_daemon_dead", lambda cfg, alias: False)

    with pytest.raises(client.ConnectError) as exc:
        client.connect(tmp_config, "nope")

    assert exc.value.retryable is False
    assert "No daemon found" in str(exc.value)


def test_should_reconnect_policy():
    cfg = Config()
    cfg.client_reconnect_attempts = 0  # unlimited
    assert client._should_reconnect(cfg, 1) is True
    assert client._should_reconnect(cfg, 1000) is True

    cfg.client_reconnect_attempts = 3
    assert client._should_reconnect(cfg, 1) is True
    assert client._should_reconnect(cfg, 3) is True
    assert client._should_reconnect(cfg, 4) is False
