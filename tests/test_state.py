"""Tests for reboot-safe alias state and USB hotplug detection."""

import asyncio
import json
import os
from pathlib import Path

import pytest
import serial

from serial_mux import state
from serial_mux.daemon import SerialDaemon


@pytest.fixture
def fake_usb_sysfs(tmp_path, monkeypatch):
    sys_devices = tmp_path / "sys" / "devices"
    sys_class_tty = tmp_path / "sys" / "class" / "tty"
    dev_dir = tmp_path / "dev"
    boot_id_path = tmp_path / "boot_id"

    usb_device = sys_devices / "pci0000:00" / "usb1" / "1-2"
    interface = usb_device / "1-2:1.0"
    tty_device = interface / "ttyUSB0"
    tty_device.mkdir(parents=True)
    (usb_device / "busnum").write_text("1\n")
    (usb_device / "devnum").write_text("4\n")

    tty_class = sys_class_tty / "ttyUSB0"
    tty_class.mkdir(parents=True)
    (tty_class / "device").symlink_to(tty_device, target_is_directory=True)
    dev_dir.mkdir()
    boot_id_path.write_text("boot-a\n")

    monkeypatch.setattr(state, "SYS_DEVICES", sys_devices)
    monkeypatch.setattr(state, "SYS_CLASS_TTY", sys_class_tty)
    monkeypatch.setattr(state, "DEV_DIR", dev_dir)
    monkeypatch.setattr(state, "BOOT_ID_PATH", boot_id_path)

    return {
        "boot_id": boot_id_path,
        "class_tty": sys_class_tty,
        "dev_dir": dev_dir,
        "interface": interface,
        "usb_device": usb_device,
    }


def test_usb_port_survives_reboot_and_resolves_new_tty(
    tmp_path, fake_usb_sysfs
):
    old_device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    usb = state.inspect_usb_device(old_device)
    info = {
        "alias": "die0",
        # Recovery must not depend on the previous runtime device name.
        "device": "/dev/ttyUSB999",
        "boot_id": "boot-a",
        **usb,
    }

    old_class = fake_usb_sysfs["class_tty"] / "ttyUSB0"
    (old_class / "device").unlink()
    old_class.rmdir()
    old_tty = fake_usb_sysfs["interface"] / "ttyUSB0"
    old_tty.rmdir()
    new_tty = fake_usb_sysfs["interface"] / "ttyUSB1"
    new_tty.mkdir()
    new_class = fake_usb_sysfs["class_tty"] / "ttyUSB1"
    new_class.mkdir()
    (new_class / "device").symlink_to(new_tty, target_is_directory=True)
    fake_usb_sysfs["boot_id"].write_text("boot-b\n")

    device, status = state.resolve_recorded_device(info)
    info_path = tmp_path / "die0.json"
    state.write_info(info_path, info)
    reconciled = state.reconcile_info(info_path, info)

    assert device == str(fake_usb_sysfs["dev_dir"] / "ttyUSB1")
    assert status == "rebooted"
    assert reconciled["device"] == device
    # Resolving is runtime-only; the persistent file never learns ttyUSB1.
    assert json.loads(info_path.read_text())["device"] is None


def test_usb_replug_in_same_boot_invalidates_mapping(fake_usb_sysfs):
    device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    usb = state.inspect_usb_device(device)
    info = {
        "alias": "die0",
        "device": "/dev/ttyUSB999",
        "boot_id": "boot-a",
        **usb,
    }
    (fake_usb_sysfs["usb_device"] / "devnum").write_text("9\n")

    resolved, status = state.resolve_recorded_device(info)

    assert resolved is None
    assert status == "replugged"


def test_usb_port_identity_normalizes_root_bus_number(fake_usb_sysfs):
    old = state.inspect_usb_tty("ttyUSB0")
    controller = fake_usb_sysfs["usb_device"].parents[1]
    usb_device = controller / "usb3" / "3-2"
    tty_device = usb_device / "3-2:1.0" / "ttyUSB1"
    tty_device.mkdir(parents=True)
    (usb_device / "busnum").write_text("3\n")
    (usb_device / "devnum").write_text("7\n")
    tty_class = fake_usb_sysfs["class_tty"] / "ttyUSB1"
    tty_class.mkdir()
    (tty_class / "device").symlink_to(tty_device, target_is_directory=True)

    new = state.inspect_usb_tty("ttyUSB1")

    assert new["usb_port"] == old["usb_port"]
    assert new["usb_instance"] != old["usb_instance"]


def test_disconnected_usb_removes_inactive_serial_only_mapping(
    tmp_path, fake_usb_sysfs
):
    device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    info = {
        "alias": "die0",
        "device": device,
        "baud": 115200,
        "pid": -1,
        "boot_id": "boot-a",
        **state.inspect_usb_device(device),
    }
    info_path = tmp_path / "die0.json"
    state.write_info(info_path, info)
    (fake_usb_sysfs["class_tty"] / "ttyUSB0" / "device").unlink()

    reconciled = state.reconcile_info(info_path, info)

    assert reconciled is None
    assert not info_path.exists()


def test_disconnected_usb_keeps_ssh_mapping(tmp_path, fake_usb_sysfs):
    device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    info = {
        "alias": "die0",
        "device": device,
        "ssh": "root@board",
        "pid": -1,
        "boot_id": "boot-a",
        **state.inspect_usb_device(device),
    }
    info_path = tmp_path / "die0.json"
    state.write_info(info_path, info)
    (fake_usb_sysfs["class_tty"] / "ttyUSB0" / "device").unlink()

    reconciled = state.reconcile_info(info_path, info)

    assert reconciled["device"] is None
    assert reconciled["ssh"] == "root@board"
    persisted = json.loads(info_path.read_text())
    assert persisted["device"] is None
    assert "usb_port" not in persisted


def test_boot_id_prevents_recycled_pid_from_looking_alive(monkeypatch):
    monkeypatch.setattr(state, "get_boot_id", lambda: "new-boot")
    kill_calls = []
    monkeypatch.setattr(state.os, "kill", lambda *args: kill_calls.append(args))

    assert not state.info_is_running(
        {"pid": os.getpid(), "boot_id": "old-boot"}
    )
    assert kill_calls == []


def test_daemon_keeps_info_but_removes_transient_files(tmp_config):
    daemon = SerialDaemon("/dev/ttyUSB9", 115200, "die0", tmp_config)
    daemon._usb_info = {
        "usb_port": "pci0000:00/usb/usb-2/usb-2:1.0",
        "usb_instance": "1:4",
    }
    daemon._write_info()
    daemon._write_pid()
    daemon._sock_path().touch()

    daemon._cleanup_files()

    assert daemon._info_path().exists()
    saved = json.loads(daemon._info_path().read_text())
    assert saved["device"] is None
    assert saved["usb_port"] == "pci0000:00/usb/usb-2/usb-2:1.0"
    assert saved["pid"] is None
    assert not daemon._pid_path().exists()
    assert not daemon._sock_path().exists()


def test_daemon_drops_empty_record_after_usb_mapping_was_cleared(tmp_config):
    daemon = SerialDaemon(None, 115200, "die0", tmp_config)
    daemon._write_info()

    daemon._cleanup_files()

    assert not daemon._info_path().exists()


def test_serial_read_propagates_disconnect_error(tmp_config):
    class DisconnectedSerial:
        is_open = True

        def read(self, _size):
            raise serial.SerialException("gone")

    daemon = SerialDaemon(None, 115200, "die0", tmp_config)
    daemon.ser = DisconnectedSerial()

    with pytest.raises(serial.SerialException, match="gone"):
        daemon._serial_read()


def test_serial_disconnect_clears_live_mapping(tmp_config, monkeypatch):
    class DisconnectedSerial:
        is_open = True

        def close(self):
            self.is_open = False

    daemon = SerialDaemon("/dev/ttyUSB0", 115200, "die0", tmp_config)
    daemon.ser = DisconnectedSerial()
    daemon.running = True
    daemon._usb_info = {"usb_port": "port", "usb_instance": "1:4"}
    # No recoverable device to poll, so the reader stops after dropping the
    # dead serial port instead of reconnecting forever.
    daemon._last_device_path = None
    daemon._write_info()
    monkeypatch.setattr(
        daemon,
        "_serial_read",
        lambda: (_ for _ in ()).throw(serial.SerialException("unplugged")),
    )

    class InlineLoop:
        async def run_in_executor(self, _executor, function):
            return function()

    monkeypatch.setattr("serial_mux.daemon.asyncio.get_event_loop", InlineLoop)
    broadcasts = []

    async def capture(message):
        broadcasts.append(message)

    monkeypatch.setattr(daemon, "_broadcast", capture)

    asyncio.run(daemon._serial_reader())

    info = json.loads(daemon._info_path().read_text())
    assert daemon.device is None
    assert info["device"] is None
    assert "usb_port" not in info
    assert broadcasts == [{"type": "serial_lost", "reason": "unplugged"}]


def test_daemon_info_contains_boot_and_usb_identity(
    tmp_config, fake_usb_sysfs, monkeypatch
):
    device = str(fake_usb_sysfs["dev_dir"] / "ttyUSB0")
    monkeypatch.setattr("serial_mux.daemon.get_boot_id", lambda: "boot-a")
    monkeypatch.setattr(
        "serial_mux.daemon.inspect_usb_device", state.inspect_usb_device
    )
    daemon = SerialDaemon(device, 115200, "die0", tmp_config)

    daemon._write_info()
    info = json.loads(daemon._info_path().read_text())

    assert info["boot_id"] == "boot-a"
    assert info["usb_port"] == "pci0000:00/usb/usb-2/usb-2:1.0"
    assert info["usb_instance"] == "1:4"
