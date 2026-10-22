"""Owner lifecycle and at-most-once input state for serial transports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from uuid import uuid4

from .transport import Transport, TransportError, TransportInfo, TransportStatus


class OwnerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    DRAINING = "draining"
    LOST = "lost"


class InputState(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InputResult:
    request_id: str
    state: InputState
    message: str = ""


class Owner:
    """Own one transport and expose explicit lifecycle transitions."""

    def __init__(
        self,
        transport: Transport,
        *,
        node_id: str = "local",
        port_id: str = "serial",
    ):
        self.transport = transport
        self.node_id = node_id
        self.port_id = port_id
        self.owner_epoch = str(uuid4())
        self.state = OwnerState.CLOSED
        self._input_results: dict[str, InputResult] = {}

    def open(self) -> None:
        if self.state == OwnerState.OPEN:
            return
        if self.state == OwnerState.DRAINING:
            raise TransportError("owner is draining")
        self.transport.open()
        self.owner_epoch = str(uuid4())
        self.state = OwnerState.OPEN

    def status(self) -> TransportInfo:
        return self.transport.status()

    def read(self, size: int = 4096) -> bytes:
        if self.state != OwnerState.OPEN:
            raise TransportError(f"owner is {self.state.value}")
        try:
            return self.transport.read(size)
        except TransportError:
            self.state = OwnerState.LOST
            raise

    def submit_input(self, request_id: str, data: bytes) -> InputResult:
        """Write once and retain the result for idempotent status lookup.

        Reusing a request ID never writes a second time. If the underlying write
        raises, delivery is unknown because the transport may have accepted data.
        """
        if not request_id:
            raise ValueError("request_id must not be empty")
        previous = self._input_results.get(request_id)
        if previous is not None:
            return previous
        if self.state != OwnerState.OPEN:
            result = InputResult(request_id, InputState.REJECTED, "owner is not open")
            self._input_results[request_id] = result
            return result
        try:
            self.transport.write(data)
        except TransportError as exc:
            self.state = OwnerState.LOST
            result = InputResult(request_id, InputState.UNKNOWN, str(exc))
        else:
            result = InputResult(request_id, InputState.ACCEPTED)
        self._input_results[request_id] = result
        return result

    def input_status(self, request_id: str) -> Optional[InputResult]:
        return self._input_results.get(request_id)

    def begin_drain(self) -> None:
        if self.state == OwnerState.OPEN:
            self.state = OwnerState.DRAINING

    def close(self) -> None:
        self.transport.close()
        self.state = OwnerState.CLOSED
        self.owner_epoch = str(uuid4())

    def set_baud(self, baud: int) -> None:
        if self.state != OwnerState.OPEN:
            raise TransportError(f"owner is {self.state.value}")
        self.transport.set_baud(baud)
