"""Transport interfaces shared by local serial and remote Agent owners."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import serial


class TransportError(RuntimeError):
    """Raised when a transport cannot perform an operation."""


class TransportStatus(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    DRAINING = "draining"
    LOST = "lost"


@dataclass(frozen=True)
class TransportInfo:
    """Snapshot of the transport state visible to the owner."""

    name: str
    status: TransportStatus
    baud: Optional[int] = None
    detail: Optional[str] = None


class Transport(ABC):
    """Synchronous byte transport contract used by an owner."""

    @abstractmethod
    def open(self) -> None:
        """Open the transport."""

    @abstractmethod
    def read(self, size: int = 4096) -> bytes:
        """Read up to ``size`` bytes."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write all data or raise ``TransportError``."""

    @abstractmethod
    def close(self) -> None:
        """Close the transport."""

    @abstractmethod
    def status(self) -> TransportInfo:
        """Return the current transport state."""

    @abstractmethod
    def set_baud(self, baud: int) -> None:
        """Change the transport baud rate."""

    def cancel(self) -> None:
        """Cancel a pending operation when the transport supports it."""
        self.close()


class SerialTransport(Transport):
    """pyserial-backed local transport."""

    def __init__(self, device: str, baud: int, *, timeout: float = 0.05):
        self.device = device
        self.baud = baud
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._status = TransportStatus.CLOSED
        self._detail: Optional[str] = None

    @property
    def serial(self) -> Optional[serial.Serial]:
        """Expose the underlying serial object for platform-specific tuning."""
        return self._serial

    def open(self) -> None:
        if self._status == TransportStatus.OPEN:
            return
        try:
            self._serial = serial.Serial(
                port=self.device,
                baudrate=self.baud,
                timeout=self.timeout,
            )
        except (serial.SerialException, OSError) as exc:
            self._status = TransportStatus.LOST
            self._detail = str(exc)
            raise TransportError(str(exc)) from exc
        self._status = TransportStatus.OPEN
        self._detail = None

    def read(self, size: int = 4096) -> bytes:
        if not self._serial or self._status != TransportStatus.OPEN:
            raise TransportError("serial transport is not open")
        try:
            return self._serial.read(size) or b""
        except (serial.SerialException, OSError) as exc:
            self._status = TransportStatus.LOST
            self._detail = str(exc)
            raise TransportError(str(exc)) from exc

    def write(self, data: bytes) -> None:
        if not self._serial or self._status != TransportStatus.OPEN:
            raise TransportError("serial transport is not open")
        try:
            self._serial.write(data)
            self._serial.flush()
        except (serial.SerialException, OSError) as exc:
            self._status = TransportStatus.LOST
            self._detail = str(exc)
            raise TransportError(str(exc)) from exc

    def close(self) -> None:
        if self._serial:
            try:
                self._serial.close()
            except (serial.SerialException, OSError) as exc:
                self._detail = str(exc)
        self._serial = None
        self._status = TransportStatus.CLOSED

    def status(self) -> TransportInfo:
        return TransportInfo(
            name=self.device,
            status=self._status,
            baud=self.baud,
            detail=self._detail,
        )

    def set_baud(self, baud: int) -> None:
        if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
            raise TransportError(f"invalid baud rate: {baud}")
        if not self._serial or self._status != TransportStatus.OPEN:
            raise TransportError("serial transport is not open")
        try:
            self._serial.baudrate = baud
        except (serial.SerialException, OSError) as exc:
            self._status = TransportStatus.LOST
            self._detail = str(exc)
            raise TransportError(str(exc)) from exc
        self.baud = baud


class FakeTransport(Transport):
    """Deterministic in-memory transport for protocol and owner tests."""

    def __init__(self, name: str = "fake", baud: int = 115200):
        self.name = name
        self.baud = baud
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self._status = TransportStatus.CLOSED
        self._detail: Optional[str] = None

    def open(self) -> None:
        if self._status == TransportStatus.DRAINING:
            raise TransportError("transport is draining")
        self._status = TransportStatus.OPEN
        self._detail = None

    def read(self, size: int = 4096) -> bytes:
        if self._status != TransportStatus.OPEN:
            raise TransportError("fake transport is not open")
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def write(self, data: bytes) -> None:
        if self._status != TransportStatus.OPEN:
            raise TransportError("fake transport is not open")
        self.outgoing.extend(data)

    def close(self) -> None:
        self._status = TransportStatus.CLOSED

    def status(self) -> TransportInfo:
        return TransportInfo(self.name, self._status, self.baud, self._detail)

    def set_baud(self, baud: int) -> None:
        if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
            raise TransportError(f"invalid baud rate: {baud}")
        if self._status != TransportStatus.OPEN:
            raise TransportError("fake transport is not open")
        self.baud = baud


class AsyncTransport:
    """Async adapter for a blocking transport."""

    def __init__(self, transport: Transport):
        self.transport = transport

    async def open(self) -> None:
        await asyncio.to_thread(self.transport.open)

    async def read(self, size: int = 4096) -> bytes:
        return await asyncio.to_thread(self.transport.read, size)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self.transport.write, data)

    async def close(self) -> None:
        await asyncio.to_thread(self.transport.close)

    async def status(self) -> TransportInfo:
        return await asyncio.to_thread(self.transport.status)

    async def set_baud(self, baud: int) -> None:
        await asyncio.to_thread(self.transport.set_baud, baud)

    async def cancel(self) -> None:
        await asyncio.to_thread(self.transport.cancel)
