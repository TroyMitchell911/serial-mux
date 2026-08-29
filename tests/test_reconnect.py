"""Tests for automatic reconnect: daemon serial re-bind and client reconnect."""

import asyncio
import json

import pytest
import serial

from serial_mux import client, state
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


USB_PORT = "pci0000:00/usb/usb-2/usb-2:1.0"


def _make_daemon(tmp_path, device="/dev/ttyUSB0"):
    cfg = Config()
    cfg.base_dir = tmp_path / "serial-mux"
    cfg.config_dir = tmp_path / "config"
    cfg.ensure_dirs()
    cfg.serial_reconnect_interval = 0.01
    daemon = SerialDaemon(device, 115200, "recon", cfg)
    return daemon, cfg


def _saved_identity():
    return {
        "usb_port": USB_PORT,
        "usb_vid": "0403",
        "usb_pid": "6001",
        "usb_serial": "FTD12345",
    }


# --- daemon: _drop_serial ---------------------------------------------------


def test_drop_serial_keeps_identity_and_broadcasts(tmp_path, monkeypatch):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon.ser = FakeSerial()
    daemon._usb_info = {
        "usb_port": USB_PORT,
        "usb_instance": "1:4",
        "usb_vid": "0403",
        "usb_pid": "6001",
        "usb_serial": "FTD12345",
    }
    daemon._usb_port = USB_PORT
    broadcasts = []

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(daemon, "_broadcast", fake_broadcast)

    asyncio.run(daemon._drop_serial("boom"))

    assert daemon.ser is None
    assert daemon.device is None
    # Identity survives (port + VID/PID + serial); only the stale instance goes.
    assert daemon._usb_info == {
        "usb_port": USB_PORT,
        "usb_vid": "0403",
        "usb_pid": "6001",
        "usb_serial": "FTD12345",
    }
    assert daemon._usb_port == USB_PORT
    assert any(m["type"] == "serial_lost" for m in broadcasts)

    # Identity stays persisted: device cleared, usb_port retained, no instance.
    info = json.loads(daemon._info_path().read_text())
    assert info["device"] is None
    assert info["usb_port"] == USB_PORT
    assert "usb_instance" not in info


# --- daemon: _wait_for_serial -------------------------------------------------


def test_wait_for_serial_without_port_gives_up(tmp_path):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon._usb_port = None
    daemon.running = True
    assert asyncio.run(daemon._wait_for_serial()) is False


def test_wait_for_serial_disabled_by_interval(tmp_path):
    daemon, cfg = _make_daemon(tmp_path)
    daemon._usb_port = USB_PORT
    daemon.running = True
    cfg.serial_reconnect_interval = 0
    assert asyncio.run(daemon._wait_for_serial()) is False


def test_identity_matches_enforces_recorded_fields(tmp_path):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon._usb_info = _saved_identity()
    base = {
        "usb_port": USB_PORT,
        "usb_instance": "1:9",
        "usb_vid": "0403",
        "usb_pid": "6001",
        "usb_serial": "FTD12345",
    }
    assert daemon._identity_matches(base) is True
    assert daemon._identity_matches({**base, "usb_serial": "OTHER"}) is False
    assert daemon._identity_matches({**base, "usb_vid": "1234"}) is False
    assert daemon._identity_matches({**base, "usb_pid": "9999"}) is False


def test_identity_matches_without_serial_is_best_effort(tmp_path):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon._usb_info = {"usb_port": USB_PORT, "usb_vid": "0403", "usb_pid": "6001"}
    # No serial recorded originally -> serial cannot be enforced.
    assert daemon._identity_matches({
        "usb_port": USB_PORT,
        "usb_vid": "0403",
        "usb_pid": "6001",
        "usb_serial": "ANYTHING",
    }) is True


# --- fake-sysfs based recovery ----------------------------------------------


def _rename_tty(fs, new_name):
    old_class = fs["class_tty"] / "ttyUSB0"
    (old_class / "device").unlink()
    old_class.rmdir()
    old_tty = fs["interface"] / "ttyUSB0"
    old_tty.rmdir()
    new_tty = fs["interface"] / new_name
    new_tty.mkdir()
    new_class = fs["class_tty"] / new_name
    new_class.mkdir()
    (new_class / "device").symlink_to(new_tty, target_is_directory=True)


def test_wait_for_serial_reconnects_same_device_new_tty(tmp_path, fake_usb_sysfs, monkeypatch):
    device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    daemon, _cfg = _make_daemon(tmp_path, device)
    daemon.running = True
    daemon._usb_info = _saved_identity()
    daemon._usb_port = USB_PORT
    _rename_tty(fake_usb_sysfs, "ttyUSB1")

    broadcasts = []

    def fake_open_serial():
        daemon.ser = FakeSerial()

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(daemon, "_open_serial", fake_open_serial)
    monkeypatch.setattr(daemon, "_broadcast", fake_broadcast)

    result = asyncio.run(daemon._wait_for_serial())

    assert result is True
    assert daemon.device == str(fake_usb_sysfs["dev_dir"] / "ttyUSB1")
    assert daemon.ser is not None
    assert any(m["type"] == "serial_restored" for m in broadcasts)


def test_wait_for_serial_rejects_different_device(tmp_path, fake_usb_sysfs, monkeypatch):
    device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    daemon, _cfg = _make_daemon(tmp_path, device)
    daemon.running = True
    daemon._usb_info = _saved_identity()
    daemon._usb_port = USB_PORT
    # A different device now sits on the same physical port.
    (fake_usb_sysfs["usb_device"] / "serial").write_text("DIFFERENT\n")

    opened = []

    def fake_open_serial():
        opened.append(True)
        daemon.ser = FakeSerial()

    sleeps = {"n": 0}

    async def fake_sleep(_interval):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            daemon.running = False

    monkeypatch.setattr(daemon, "_open_serial", fake_open_serial)
    monkeypatch.setattr("serial_mux.daemon.asyncio.sleep", fake_sleep)

    result = asyncio.run(daemon._wait_for_serial())

    assert result is False
    assert opened == []  # the wrong device was never bound
    assert sleeps["n"] >= 3


def test_find_usb_device_rejects_ambiguous_match(fake_usb_sysfs):
    # Expose a second TTY under the same USB interface so the physical port
    # maps to two device nodes.
    second_tty = fake_usb_sysfs["interface"] / "ttyUSB1"
    second_tty.mkdir()
    second_class = fake_usb_sysfs["class_tty"] / "ttyUSB1"
    second_class.mkdir()
    (second_class / "device").symlink_to(second_tty, target_is_directory=True)

    device, metadata = state.find_usb_device(USB_PORT)

    assert device is None
    assert metadata == {}


# --- daemon: _serial_reader recovery loop ------------------------------------


def test_serial_reader_recovers_after_serial_loss(tmp_path, monkeypatch):
    daemon, _cfg = _make_daemon(tmp_path)
    daemon.running = True
    daemon.ser = FakeSerial()
    daemon._usb_port = USB_PORT
    broadcasts = []
    reads = {"n": 0}

    def fake_serial_read():
        reads["n"] += 1
        if reads["n"] == 1:
            raise serial.SerialException("device vanished")
        if reads["n"] >= 4:
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
    assert reads["n"] >= 4


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
