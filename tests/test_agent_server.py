"""Integration tests for the authenticated Agent protocol server."""

import asyncio
import base64

import pytest

from serial_mux.agent import PortDefinition, PortManager
from serial_mux.agent_server import AgentProtocolError, AgentServer
from serial_mux.identity import NodeIdentityStore
from serial_mux.protocol import async_read_msg, async_write_msg, make_envelope
from serial_mux.transport import FakeTransport


class PeerNames:
    def __init__(self):
        self.values = iter([("127.0.0.1", 12345)])


def make_server(tmp_path, *, authenticated=True):
    manager = PortManager("node-a", "Node A")
    transport = FakeTransport("COM-test")
    manager.register(PortDefinition("port-a", "COM-test"), transport)
    manager.share("port-a")
    identity = NodeIdentityStore(tmp_path / "node.json").load_or_create()
    history = [
        {"port_id": "port-a", "owner_epoch": "epoch-1", "seq": 1, "data": base64.b64encode(b"old").decode()},
        {"port_id": "port-a", "owner_epoch": "epoch-1", "seq": 2, "data": base64.b64encode(b"new").decode()},
    ]
    server = AgentServer(
        manager,
        identity,
        authenticate_peer=(lambda _node, _fingerprint: authenticated),
        history_provider=lambda port_id, from_seq: [
            chunk for chunk in history if chunk["seq"] > from_seq
        ],
    )
    return server, manager, transport


async def handshake(reader, writer, *, fingerprint="peer"):
    await async_write_msg(writer, make_envelope(
        "hello",
        {
            "node_id": "node-b",
            "fingerprint": fingerprint,
            "capabilities": ["request_id", "heartbeat"],
        },
        request_id="hello-1",
    ))
    response = await async_read_msg(reader)
    assert response["type"] == "hello_ack"
    assert response["payload"]["authenticated"] is True
    assert "request_id" in response["payload"]["capabilities"]


@pytest.mark.timeout(30)
def test_agent_server_requires_authentication(tmp_path):
    async def run():
        server, _manager, _transport = make_server(tmp_path, authenticated=False)
        host, port = await server.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            await async_write_msg(writer, make_envelope(
                "hello",
                {"node_id": "node-b", "fingerprint": "bad", "capabilities": []},
                request_id="hello-1",
            ))
            response = await async_read_msg(reader)
            assert response["type"] == "error"
            assert response["payload"]["code"] == "authentication_failed"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(run())


@pytest.mark.timeout(30)
def test_agent_server_lists_opens_sends_input_and_pings(tmp_path):
    async def run():
        server, manager, transport = make_server(tmp_path)
        host, port = await server.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            await handshake(reader, writer)

            await async_write_msg(writer, make_envelope("list_ports", request_id="list-1"))
            response = await async_read_msg(reader)
            assert response["type"] == "port_info"
            assert response["request_id"] == "list-1"
            assert response["payload"]["ports"][0]["port_id"] == "port-a"

            await async_write_msg(writer, make_envelope(
                "open",
                {"port_id": "port-a"},
                request_id="open-1",
            ))
            response = await async_read_msg(reader)
            assert response["type"] == "open_ack"
            assert response["payload"]["port"]["state"] == "open"

            await async_write_msg(writer, make_envelope(
                "input",
                {"port_id": "port-a", "data": base64.b64encode(b"hello").decode()},
                request_id="input-1",
            ))
            response = await async_read_msg(reader)
            assert response["type"] == "input_status"
            assert response["request_id"] == "input-1"
            assert response["payload"]["state"] == "accepted"
            assert bytes(transport.outgoing) == b"hello"

            await async_write_msg(writer, make_envelope("ping"))
            response = await async_read_msg(reader)
            assert response["type"] == "pong"

            sequence = await server.publish_output("port-a", b"line\n")
            response = await async_read_msg(reader)
            assert response["type"] == "output"
            assert response["broadcast"] is True
            assert response["payload"]["seq"] == sequence
            assert base64.b64decode(response["payload"]["data"]) == b"line\n"

            writer.close()
            await writer.wait_closed()
            await server.stop()
            assert manager.info("port-a").state.value == "available"
        finally:
            await server.stop()

    asyncio.run(run())


@pytest.mark.timeout(30)
def test_agent_server_replays_history_from_sequence(tmp_path):
    async def run():
        server, _manager, _transport = make_server(tmp_path)
        host, port = await server.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            await handshake(reader, writer)
            await async_write_msg(writer, make_envelope(
                "history_request",
                {"port_id": "port-a", "from_seq": 1},
                request_id="history-1",
            ))
            chunk = await async_read_msg(reader)
            complete = await async_read_msg(reader)
            assert chunk["type"] == "history_chunk"
            assert chunk["payload"]["seq"] == 2
            assert complete["type"] == "history_complete"
            assert complete["request_id"] == "history-1"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(run())


def test_agent_server_rejects_remote_listener_without_tls(tmp_path):
    server, _manager, _transport = make_server(tmp_path)

    async def run():
        with pytest.raises(AgentProtocolError, match="TLS"):
            await server.start("0.0.0.0")

    asyncio.run(run())
