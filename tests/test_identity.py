"""Tests for node identity, pairing codes, and peer trust."""

import pytest

from serial_mux.identity import (
    IdentityError,
    NodeIdentityStore,
    PairingCodeStore,
    PairingStore,
)


def test_node_identity_is_stable_and_private(tmp_path):
    path = tmp_path / "identity.json"
    store = NodeIdentityStore(path)

    first = store.load_or_create()
    second = store.load_or_create()

    assert first == second
    assert first.node_id.startswith("node-")
    assert len(first.secret) >= 32
    assert first.fingerprint == second.fingerprint
    assert (path.stat().st_mode & 0o777) == 0o600


def test_node_identity_rejects_tampering(tmp_path):
    path = tmp_path / "identity.json"
    identity = NodeIdentityStore(path).load_or_create()
    path.write_text(path.read_text().replace(identity.node_id, "node-other", 1))

    with pytest.raises(IdentityError, match="fingerprint"):
        NodeIdentityStore(path).load_or_create()


def test_pairing_code_is_one_time_and_expires(tmp_path):
    now = 1000.0

    def clock():
        return now_holder[0]

    now_holder = [now]
    store = PairingCodeStore(tmp_path / "pairing.json", clock=clock)
    issued = store.issue(ttl=10)

    assert store.consume(issued.token_id, "000000") is False
    assert store.consume(issued.token_id, issued.code) is True
    assert store.consume(issued.token_id, issued.code) is False

    again = store.issue(ttl=10)
    now_holder[0] = now + 11
    assert store.consume(again.token_id, again.code) is False


def test_pairing_store_trust_revoke_and_persistence(tmp_path):
    path = tmp_path / "peers.json"
    store = PairingStore(path, clock=lambda: 10)
    peer = store.trust(
        "node-b",
        name="Peer B",
        fingerprint="abc123",
        credential_ref="cred:peer-b",
    )

    assert store.is_trusted("node-b", "abc123") is True
    assert store.is_trusted("node-b", "other") is False

    store.revoke("node-b")
    assert store.is_trusted("node-b", "abc123") is False

    reloaded = PairingStore(path, clock=lambda: 20)
    assert reloaded.get("node-b") == store.get("node-b")
    assert reloaded.get("node-b").revoked is True
    assert reloaded.get("node-b").paired_at == 10
    assert peer.credential_ref == "cred:peer-b"
