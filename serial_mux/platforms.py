"""Platform adapters for serial enumeration, local IPC, and terminal input."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import serial
from serial.tools import list_ports


@dataclass(frozen=True)
class SerialPortDescriptor:
    """Portable serial port metadata exposed by an Agent."""

    port_id: str
    name: str
    description: str
    hardware_id: Optional[str]


class SerialProvider:
    """Platform-neutral serial provider interface."""

    def enumerate(self) -> Iterable[SerialPortDescriptor]:
        raise NotImplementedError

    def open(self, descriptor: SerialPortDescriptor, baud: int) -> serial.Serial:
        return serial.Serial(descriptor.name, baudrate=baud, timeout=0.05)


class WindowsSerialProvider(SerialProvider):
    """Enumerate Windows COM ports with stable hardware identifiers."""

    def enumerate(self) -> list[SerialPortDescriptor]:
        ports = []
        for port in list_ports.comports():
            hardware_id = port.hwid or None
            port_id = hardware_id or f"serial:{port.name}"
            ports.append(
                SerialPortDescriptor(
                    port_id=port_id,
                    name=port.device,
                    description=port.description or "",
                    hardware_id=hardware_id,
                )
            )
        return sorted(ports, key=lambda port: port.name)


class PosixSerialProvider(SerialProvider):
    """Enumerate serial ports using pyserial's cross-platform discovery."""

    def enumerate(self) -> list[SerialPortDescriptor]:
        return [
            SerialPortDescriptor(
                port_id=port.hwid or f"serial:{port.device}",
                name=port.device,
                description=port.description or "",
                hardware_id=port.hwid or None,
            )
            for port in sorted(list_ports.comports(), key=lambda item: item.device)
        ]


def default_serial_provider(platform_name: Optional[str] = None) -> SerialProvider:
    name = platform_name or os.name
    return WindowsSerialProvider() if name == "nt" else PosixSerialProvider()


class ProcessSupervisor:
    """Small lifecycle adapter that avoids POSIX-only fork assumptions."""

    def __init__(self, process: subprocess.Popen):
        self.process = process

    @classmethod
    def start(cls, argv: list[str], **kwargs) -> "ProcessSupervisor":
        return cls(subprocess.Popen(argv, **kwargs))

    @property
    def pid(self) -> int:
        return self.process.pid

    def is_running(self) -> bool:
        return self.process.poll() is None

    def terminate(self, timeout: float = 3.0) -> None:
        if not self.is_running():
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)


class LocalIPC:
    """Describe the local IPC family selected for the current platform.

    ``platform_name`` is injectable so both branches can be exercised without
    patching ``os.name`` globally, which would break ``pathlib`` on the host.
    """

    def __init__(self, platform_name: Optional[str] = None):
        self._platform_name = platform_name or os.name

    @property
    def kind(self) -> str:
        return "named-pipe" if self._platform_name == "nt" else "unix-socket"

    def endpoint(self, base_dir: Path, alias: str) -> str:
        if self._platform_name == "nt":
            return rf"\\.\pipe\serial-mux-{alias}"
        return str(Path(base_dir) / "sock" / f"{alias}.sock")


class WindowsTerminalAdapter:
    """Minimal Windows console adapter boundary for future interactive mode."""

    detach_byte = b"\x1d"

    def read_byte(self) -> bytes:
        if os.name != "nt":
            raise OSError("Windows terminal adapter requires Windows")
        import msvcrt

        return msvcrt.getwch().encode("utf-8", errors="replace")

    def is_interactive(self) -> bool:
        return os.name == "nt" and os.isatty(0)


class SignalAdapter:
    """Normalize graceful-stop registration across supported platforms."""

    def install(self, callback) -> None:
        signal.signal(signal.SIGINT, lambda _signum, _frame: callback())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda _signum, _frame: callback())
