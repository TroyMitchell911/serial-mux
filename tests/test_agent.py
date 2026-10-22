"""Tests for Agent port ownership and sharing policy."""

import pytest

from serial_mux.agent import PortDefinition, PortError, PortManager, PortState
from serial_mux.transport import FakeTransport, TransportError


def make_manager():
    manager = PortManager("node-a", "Node A")
    transport = FakeTransport("COM-test")
    manager.register(
        PortDefinition(
            port_id="port-a",
            name="COM-test",
            hardware_id="usb:123",
            description="test port",
        ),
        transport,
    )
    return manager, transport


def test_registered_ports_are_not_shared_by_default():
    manager, _ = make_manager()

    local = manager.list_ports()
    remote = manager.list_ports(remote=True)

    assert local[0].shared is False
    assert local[0].state == PortState.AVAILABLE
    assert remote == []


def test_share_then_remote_open_and_input():
    manager, transport = make_manager()
    manager.share("port-a")

    info = manager.open("port-a", remote=True)
    result = manager.write("port-a", "request-1", b"hello")

    assert info.state == PortState.OPEN
    assert result.state.value == "accepted"
    assert bytes(transport.outgoing) == b"hello"
    assert manager.list_ports(remote=True)[0].port_id == "port-a"


def test_remote_open_rejects_unshared_port():
    manager, _ = make_manager()

    with pytest.raises(PortError, match="not shared"):
        manager.open("port-a", remote=True)


def test_unshare_drains_open_port_and_closes_after_grace_period():
    manager, _ = make_manager()
    manager.share("port-a")
    manager.open("port-a", remote=True)

    info = manager.unshare("port-a")
    assert info.state == PortState.DRAINING
    assert info.shared is False

    with pytest.raises(PortError, match="not shared"):
        manager.write("port-a", "request-1", b"blocked")

    info = manager.finish_drain("port-a")
    assert info.state == PortState.AVAILABLE
    assert info.shared is False


def test_duplicate_port_identity_is_rejected():
    manager, _ = make_manager()

    with pytest.raises(PortError, match="already registered"):
        manager.register(PortDefinition("port-a", "other"), FakeTransport())


def test_failed_open_marks_port_offline():
    class FailingTransport(FakeTransport):
        def open(self):
            raise TransportError("device unavailable")

    manager = PortManager("node-a")
    manager.register(PortDefinition("port-a", "fake"), FailingTransport())
    manager.share("port-a")

    with pytest.raises(PortError, match="device unavailable"):
        manager.open("port-a", remote=True)
    assert manager.info("port-a").state == PortState.OFFLINE
