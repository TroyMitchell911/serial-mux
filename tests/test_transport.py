"""Tests for transport and owner lifecycle contracts."""

import pytest

from serial_mux.ownership import InputState, Owner, OwnerState
from serial_mux.transport import FakeTransport, TransportError, TransportStatus


def test_fake_transport_round_trip_and_baud():
    transport = FakeTransport("tty-test")
    transport.open()
    transport.incoming.extend(b"device")

    assert transport.read() == b"device"
    transport.write(b"command")
    transport.set_baud(9600)

    assert bytes(transport.outgoing) == b"command"
    assert transport.status().baud == 9600
    assert transport.status().status == TransportStatus.OPEN


def test_owner_assigns_new_epoch_and_accepts_input_once():
    transport = FakeTransport()
    owner = Owner(transport, node_id="node-a", port_id="port-a")
    initial_epoch = owner.owner_epoch

    owner.open()
    result = owner.submit_input("request-1", b"hello")
    duplicate = owner.submit_input("request-1", b"different")

    assert owner.state == OwnerState.OPEN
    assert owner.owner_epoch != initial_epoch
    assert result.state == InputState.ACCEPTED
    assert duplicate == result
    assert bytes(transport.outgoing) == b"hello"


def test_owner_rejects_input_while_draining():
    transport = FakeTransport()
    owner = Owner(transport)
    owner.open()
    owner.begin_drain()

    result = owner.submit_input("request-1", b"hello")

    assert owner.state == OwnerState.DRAINING
    assert result.state == InputState.REJECTED
    assert transport.outgoing == bytearray()


def test_owner_write_failure_is_unknown_and_not_retried(monkeypatch):
    transport = FakeTransport()
    owner = Owner(transport)
    owner.open()
    calls = []

    def fail(data):
        calls.append(data)
        raise TransportError("connection lost")

    monkeypatch.setattr(transport, "write", fail)
    first = owner.submit_input("request-1", b"hello")
    second = owner.submit_input("request-1", b"hello")

    assert first.state == InputState.UNKNOWN
    assert second == first
    assert calls == [b"hello"]
    assert owner.state == OwnerState.LOST


def test_owner_closed_transport_rejects_open_write():
    owner = Owner(FakeTransport())

    result = owner.submit_input("request-1", b"hello")

    assert result.state == InputState.REJECTED
    with pytest.raises(TransportError, match="closed"):
        owner.read()
