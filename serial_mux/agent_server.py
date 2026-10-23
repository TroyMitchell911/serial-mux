"""Authenticated asyncio protocol service for Agent-managed ports."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Optional
from uuid import uuid4

from .agent import PortError, PortInfo, PortManager
from .identity import NodeIdentity
from .protocol import (
    CAPABILITIES,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    ProtocolError,
    async_read_msg,
    async_write_msg,
    make_envelope,
    negotiate_capabilities,
    negotiate_version,
    validate_envelope,
)


class AgentProtocolError(RuntimeError):
    """Raised when an Agent request is invalid or unauthorized."""


PeerAuthenticator = Callable[[str, str], bool | Awaitable[bool]]
HistoryProvider = Callable[[str, int], Iterable[dict] | Awaitable[Iterable[dict]]]


class AgentServer:
    """Serve one ``PortManager`` over an authenticated stream protocol.

    Insecure connections are accepted only on loopback and only when explicitly
    enabled. Non-loopback operation requires a TLS context and peer callback.
    """

    def __init__(
        self,
        manager: PortManager,
        identity: NodeIdentity,
        *,
        node_name: str = "",
        authenticate_peer: Optional[PeerAuthenticator] = None,
        history_provider: Optional[HistoryProvider] = None,
        allow_insecure_loopback: bool = False,
        clock=time.time,
    ):
        self.manager = manager
        self.identity = identity
        self.node_name = node_name or manager.node_name
        self.authenticate_peer = authenticate_peer
        self.history_provider = history_provider
        self.allow_insecure_loopback = allow_insecure_loopback
        self.clock = clock
        self._server: Optional[asyncio.AbstractServer] = None
        self._sessions: set[AgentSession] = set()
        self._port_users: dict[str, set[AgentSession]] = {}
        self._sequences: dict[str, int] = {}

    async def start(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        ssl_context=None,
    ) -> tuple[str, int]:
        """Start listening; return the bound address."""
        loopback = _is_loopback(host)
        if not loopback and ssl_context is None:
            raise AgentProtocolError("TLS is required for non-loopback Agent listeners")
        if not loopback and self.authenticate_peer is None:
            raise AgentProtocolError("peer authentication is required for remote listeners")
        self._server = await asyncio.start_server(
            self._handle_connection,
            host,
            port,
            ssl=ssl_context,
        )
        socket = self._server.sockets[0].getsockname()
        return socket[0], socket[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        sessions = list(self._sessions)
        await asyncio.gather(*(session.close() for session in sessions))

    async def publish_output(self, port_id: str, data: bytes) -> int:
        """Broadcast one output event and return its sequence number."""
        info = self.manager.info(port_id)
        sequence = self._sequences.get(port_id, 0) + 1
        self._sequences[port_id] = sequence
        message = make_envelope(
            "output",
            {
                "node_id": info.node_id,
                "port_id": info.port_id,
                "owner_epoch": info.owner_epoch,
                "seq": sequence,
                "data": base64.b64encode(data).decode("ascii"),
            },
            connection_id=info.owner_epoch,
            broadcast=True,
        )
        for session in list(self._sessions):
            if port_id in session.open_ports:
                await session.send(message)
        return sequence

    async def _handle_connection(self, reader, writer) -> None:
        session = AgentSession(self, reader, writer)
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)

    async def _history(self, port_id: str, from_seq: int) -> list[dict]:
        if self.history_provider is None:
            return []
        result = self.history_provider(port_id, from_seq)
        if asyncio.iscoroutine(result):
            result = await result
        return list(result)


class AgentSession:
    def __init__(self, server: AgentServer, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.connection_id = str(uuid4())
        self.node_id: Optional[str] = None
        self.capabilities = frozenset()
        self.open_ports: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def run(self) -> None:
        try:
            hello = await asyncio.wait_for(async_read_msg(self.reader), timeout=5)
            validate_envelope(hello)
            await self._hello(hello)
            while True:
                message = await async_read_msg(self.reader)
                if message is None:
                    return
                validate_envelope(message)
                await self._dispatch(message)
        except (
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
            ConnectionError,
            ProtocolError,
            AgentProtocolError,
            PortError,
        ):
            return
        finally:
            await self.close()

    async def send(self, message: dict) -> None:
        if self._closed:
            return
        async with self._write_lock:
            await async_write_msg(self.writer, message)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for port_id in list(self.open_ports):
            users = self.server._port_users.get(port_id, set())
            users.discard(self)
            if not users:
                self.server._port_users.pop(port_id, None)
                try:
                    self.server.manager.close(port_id)
                except PortError:
                    pass
            self.open_ports.discard(port_id)
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _hello(self, message: dict) -> None:
        version = message["protocol"]
        compatible, code = negotiate_version(version)
        if not compatible:
            await self._error(message, code, "protocol version is incompatible")
            raise AgentProtocolError(code)
        payload = message.get("payload", {})
        self.node_id = payload.get("node_id")
        fingerprint = payload.get("fingerprint")
        if not self.node_id or not fingerprint:
            await self._error(message, "authentication_required", "peer identity is required")
            raise AgentProtocolError("authentication_required")
        if not await self._is_authenticated(self.node_id, fingerprint):
            await self._error(message, "authentication_failed", "peer is not trusted")
            raise AgentProtocolError("authentication_failed")
        self.capabilities = negotiate_capabilities(payload.get("capabilities", []))
        await self.send(
            make_envelope(
                "hello_ack",
                {
                    "node_id": self.server.identity.node_id,
                    "node_name": self.server.node_name,
                    "fingerprint": self.server.identity.fingerprint,
                    "protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
                    "capabilities": sorted(CAPABILITIES),
                    "authenticated": True,
                },
                connection_id=self.connection_id,
            )
        )

    async def _is_authenticated(self, node_id: str, fingerprint: str) -> bool:
        if self.server.authenticate_peer is not None:
            result = self.server.authenticate_peer(node_id, fingerprint)
            if asyncio.iscoroutine(result):
                return bool(await result)
            return bool(result)
        if not self.server.allow_insecure_loopback:
            return False
        peer = self.writer.get_extra_info("peername")
        return bool(peer and _is_loopback(str(peer[0])))

    async def _dispatch(self, message: dict) -> None:
        msg_type = message["type"]
        if msg_type == "ping":
            await self.send(make_envelope("pong", {"time": self.server.clock()}))
            return
        if msg_type == "list_ports":
            await self._reply(message, "port_info", {
                "ports": [_port_dict(info) for info in self.server.manager.list_ports(remote=True)],
            })
            return
        if msg_type == "open":
            port_id = self._port_id(message)
            users = self.server._port_users.setdefault(port_id, set())
            info = self.server.manager.info(port_id)
            if not users:
                info = self.server.manager.open(port_id, remote=True)
            elif not info.shared:
                raise PortError("port is not shared")
            users.add(self)
            self.open_ports.add(port_id)
            await self._reply(message, "open_ack", {"port": _port_dict(info)})
            return
        if msg_type == "close":
            port_id = self._port_id(message)
            users = self.server._port_users.get(port_id, set())
            users.discard(self)
            self.open_ports.discard(port_id)
            if not users:
                self.server._port_users.pop(port_id, None)
                info = self.server.manager.close(port_id)
            else:
                info = self.server.manager.info(port_id)
            await self._reply(message, "close_ack", {"port": _port_dict(info)})
            return
        if msg_type == "input":
            port_id = self._port_id(message)
            payload = message["payload"]
            request_id = message.get("request_id")
            if not request_id:
                await self._error(message, "request_id_required", "input requires request_id")
                return
            try:
                data = base64.b64decode(payload.get("data", ""), validate=True)
            except (TypeError, binascii.Error):
                await self._error(message, "invalid_data", "input data is not valid base64")
                return
            result = self.server.manager.write(port_id, request_id, data)
            await self._reply(message, "input_status", {
                "port_id": port_id,
                "request_id": result.request_id,
                "state": result.state.value,
                "message": result.message,
            })
            return
        if msg_type == "set_baud":
            port_id = self._port_id(message)
            info = self.server.manager.set_baud(port_id, message["payload"].get("baud"))
            await self._reply(message, "baud_ack", {"port": _port_dict(info)})
            return
        if msg_type == "history_request":
            port_id = self._port_id(message)
            from_seq = int(message["payload"].get("from_seq", 0))
            for chunk in await self.server._history(port_id, from_seq):
                await self.send(make_envelope("history_chunk", chunk, connection_id=self.connection_id))
            await self._reply(message, "history_complete", {"port_id": port_id})
            return
        await self._error(message, "unknown_message", f"unknown message type: {msg_type}")

    async def _reply(self, request: dict, msg_type: str, payload: dict) -> None:
        await self.send(make_envelope(
            msg_type,
            payload,
            request_id=request.get("request_id"),
            connection_id=self.connection_id,
        ))

    async def _error(self, request: dict, code: str, message: str) -> None:
        await self.send(make_envelope(
            "error",
            {"code": code, "message": message, "retryable": False},
            request_id=request.get("request_id"),
            connection_id=self.connection_id,
        ))

    @staticmethod
    def _port_id(message: dict) -> str:
        port_id = message.get("payload", {}).get("port_id")
        if not isinstance(port_id, str) or not port_id:
            raise AgentProtocolError("port_id is required")
        return port_id


def _port_dict(info: PortInfo) -> dict:
    return {
        "node_id": info.node_id,
        "node_name": info.node_name,
        "port_id": info.port_id,
        "name": info.name,
        "hardware_id": info.hardware_id,
        "description": info.description,
        "baud": info.baud,
        "state": info.state.value,
        "shared": info.shared,
        "owner_epoch": info.owner_epoch,
        "detail": info.detail,
    }


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "::1"}
