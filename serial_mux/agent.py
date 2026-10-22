"""Agent-side port ownership and sharing primitives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Iterable, Optional

from .ownership import Owner, OwnerState
from .transport import Transport, TransportError, TransportInfo


class PortState(str, Enum):
    AVAILABLE = "available"
    OPEN = "open"
    DRAINING = "draining"
    OFFLINE = "offline"
    REVOKED = "revoked"


@dataclass(frozen=True)
class PortDefinition:
    """Stable identity and display metadata for one managed serial port."""

    port_id: str
    name: str
    hardware_id: Optional[str] = None
    description: str = ""
    baud: int = 115200


@dataclass(frozen=True)
class PortInfo:
    """Port state exposed to local or remote Agent clients."""

    node_id: str
    node_name: str
    port_id: str
    name: str
    hardware_id: Optional[str]
    description: str
    baud: int
    state: PortState
    shared: bool
    owner_epoch: Optional[str] = None
    detail: Optional[str] = None


class PortError(RuntimeError):
    """Raised when an Agent port operation is invalid."""


@dataclass
class _ManagedPort:
    definition: PortDefinition
    owner: Owner
    shared: bool = False
    state: PortState = PortState.AVAILABLE
    detail: Optional[str] = None


class PortManager:
    """Own and publish a set of local serial ports.

    A port is registered with exactly one transport instance. ``open`` never
    creates another transport, which prevents a second process-level owner from
    being hidden behind the Agent API.
    """

    def __init__(self, node_id: str, node_name: str = ""):
        if not node_id:
            raise ValueError("node_id must not be empty")
        self.node_id = node_id
        self.node_name = node_name or node_id
        self._ports: dict[str, _ManagedPort] = {}
        self.audit_log: list[dict] = []

    def register(
        self,
        definition: PortDefinition,
        transport: Transport | Callable[[], Transport],
    ) -> None:
        """Register one port and reject duplicate stable identities."""
        if not definition.port_id:
            raise ValueError("port_id must not be empty")
        if definition.port_id in self._ports:
            raise PortError(f"port already registered: {definition.port_id}")
        instance = transport() if callable(transport) else transport
        self._ports[definition.port_id] = _ManagedPort(
            definition=definition,
            owner=Owner(instance, node_id=self.node_id, port_id=definition.port_id),
        )
        self._audit("register", definition.port_id)

    def unregister(self, port_id: str) -> None:
        managed = self._get(port_id)
        if managed.owner.state not in {OwnerState.CLOSED, OwnerState.LOST}:
            managed.owner.close()
        del self._ports[port_id]
        self._audit("unregister", port_id)

    def list_ports(self, *, remote: bool = False) -> list[PortInfo]:
        """List ports, hiding unshared entries from remote peers."""
        return [
            self.info(port_id)
            for port_id, managed in sorted(self._ports.items())
            if not remote or managed.shared
        ]

    def info(self, port_id: str) -> PortInfo:
        managed = self._get(port_id)
        owner = managed.owner
        return PortInfo(
            node_id=self.node_id,
            node_name=self.node_name,
            port_id=managed.definition.port_id,
            name=managed.definition.name,
            hardware_id=managed.definition.hardware_id,
            description=managed.definition.description,
            baud=managed.definition.baud,
            state=managed.state,
            shared=managed.shared,
            owner_epoch=owner.owner_epoch if owner.state != OwnerState.CLOSED else None,
            detail=managed.detail or owner.status().detail,
        )

    def share(self, port_id: str) -> PortInfo:
        managed = self._get(port_id)
        if managed.state in {PortState.OFFLINE, PortState.REVOKED}:
            raise PortError(f"cannot share {managed.state.value} port")
        managed.shared = True
        if managed.state == PortState.DRAINING:
            managed.state = self._state_from_owner(managed.owner)
        self._audit("share", port_id)
        return self.info(port_id)

    def unshare(self, port_id: str) -> PortInfo:
        managed = self._get(port_id)
        managed.shared = False
        if managed.owner.state == OwnerState.OPEN:
            managed.owner.begin_drain()
            managed.state = PortState.DRAINING
        self._audit("unshare", port_id)
        return self.info(port_id)

    def open(self, port_id: str, *, remote: bool = False) -> PortInfo:
        managed = self._get(port_id)
        if remote and not managed.shared:
            raise PortError("port is not shared")
        if managed.state in {PortState.OFFLINE, PortState.REVOKED}:
            raise PortError(f"port is {managed.state.value}")
        if managed.owner.state == OwnerState.DRAINING:
            raise PortError("port is draining")
        try:
            managed.owner.open()
        except Exception as exc:
            managed.state = PortState.OFFLINE
            managed.detail = str(exc)
            raise PortError(str(exc)) from exc
        managed.state = PortState.OPEN
        managed.detail = None
        self._audit("open", port_id, remote=remote)
        return self.info(port_id)

    def close(self, port_id: str) -> PortInfo:
        managed = self._get(port_id)
        managed.owner.close()
        managed.state = PortState.AVAILABLE
        managed.detail = None
        self._audit("close", port_id)
        return self.info(port_id)

    def finish_drain(self, port_id: str) -> PortInfo:
        managed = self._get(port_id)
        if managed.state == PortState.DRAINING:
            managed.owner.close()
            managed.state = PortState.AVAILABLE
            managed.detail = None
            self._audit("finish_drain", port_id)
        return self.info(port_id)

    def write(self, port_id: str, request_id: str, data: bytes):
        managed = self._get(port_id)
        if not managed.shared:
            raise PortError("port is not shared")
        if managed.state != PortState.OPEN:
            raise PortError(f"port is {managed.state.value}")
        return managed.owner.submit_input(request_id, data)

    def read(self, port_id: str, size: int = 4096) -> bytes:
        managed = self._get(port_id)
        try:
            data = managed.owner.read(size)
        except TransportError as exc:
            managed.state = PortState.OFFLINE
            managed.detail = str(exc)
            raise PortError(str(exc)) from exc
        return data

    def set_baud(self, port_id: str, baud: int) -> PortInfo:
        managed = self._get(port_id)
        try:
            managed.owner.set_baud(baud)
        except TransportError as exc:
            managed.state = PortState.OFFLINE
            managed.detail = str(exc)
            raise PortError(str(exc)) from exc
        managed.definition = replace(managed.definition, baud=baud)
        self._audit("set_baud", port_id, baud=baud)
        return self.info(port_id)

    def mark_offline(self, port_id: str, detail: str = "") -> PortInfo:
        managed = self._get(port_id)
        managed.owner.close()
        managed.state = PortState.OFFLINE
        managed.detail = detail or None
        self._audit("offline", port_id, detail=detail)
        return self.info(port_id)

    def _get(self, port_id: str) -> _ManagedPort:
        try:
            return self._ports[port_id]
        except KeyError as exc:
            raise PortError(f"unknown port: {port_id}") from exc

    @staticmethod
    def _state_from_owner(owner: Owner) -> PortState:
        return {
            OwnerState.CLOSED: PortState.AVAILABLE,
            OwnerState.OPEN: PortState.OPEN,
            OwnerState.DRAINING: PortState.DRAINING,
            OwnerState.LOST: PortState.OFFLINE,
        }[owner.state]

    def _audit(self, action: str, port_id: str, **fields) -> None:
        event = {"action": action, "port_id": port_id}
        event.update(fields)
        self.audit_log.append(event)
