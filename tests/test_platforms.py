"""Tests for platform adapter boundaries."""

from types import SimpleNamespace

import pytest

from serial_mux import platforms


def test_windows_provider_prefers_hardware_identity(monkeypatch):
    ports = [
        SimpleNamespace(
            device="COM3",
            name="COM3",
            description="USB Serial Port",
            hwid="USB VID:PID=1234:5678 SER=ABC",
        ),
        SimpleNamespace(device="COM2", name="COM2", description="Onboard", hwid=None),
    ]
    monkeypatch.setattr(platforms.list_ports, "comports", lambda: ports)

    provider = platforms.WindowsSerialProvider()
    descriptors = provider.enumerate()

    assert descriptors[0].name == "COM2"
    assert descriptors[0].port_id == "serial:COM2"
    assert descriptors[1].port_id == "USB VID:PID=1234:5678 SER=ABC"
    assert descriptors[1].hardware_id == descriptors[1].port_id


def test_default_serial_provider_selects_correct_class():
    posix_provider = platforms.default_serial_provider("posix")
    nt_provider = platforms.default_serial_provider("nt")
    assert isinstance(posix_provider, platforms.PosixSerialProvider)
    assert isinstance(nt_provider, platforms.WindowsSerialProvider)


def test_local_ipc_selects_platform_endpoint(tmp_path):
    posix_ipc = platforms.LocalIPC(platform_name="posix")
    assert posix_ipc.kind == "unix-socket"
    # The posix branch joins with the host path separator, so build the
    # expectation with pathlib instead of hard-coding "sock/die0.sock".
    assert posix_ipc.endpoint(tmp_path, "die0") == str(
        tmp_path / "sock" / "die0.sock"
    )

    nt_ipc = platforms.LocalIPC(platform_name="nt")
    assert nt_ipc.kind == "named-pipe"
    assert nt_ipc.endpoint(tmp_path, "die0") == r"\\.\pipe\serial-mux-die0"


def test_process_supervisor_terminates_running_process():
    import subprocess
    import sys

    supervisor = platforms.ProcessSupervisor.start([
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    ])
    try:
        assert supervisor.is_running() is True
        supervisor.terminate(timeout=2)
        assert supervisor.is_running() is False
    finally:
        if supervisor.is_running():
            supervisor.process.kill()
            supervisor.process.wait()
