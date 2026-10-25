"""Persistent node identity and one-time pairing state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PRIVATE_FILE_MODE = 0o600


class IdentityError(RuntimeError):
    """Raised when identity or pairing state is invalid."""


def restrict_permissions(path: Path, mode: int = PRIVATE_FILE_MODE) -> bool:
    """Apply private POSIX permissions to a secret-bearing file.

    Windows has no POSIX mode bits; ``os.chmod`` there only toggles the
    read-only attribute, so the caller must rely on the parent directory ACL
    instead. The return value says whether real mode bits were applied, which
    lets tests assert the policy per platform without weakening it on POSIX.
    """
    if os.name != "posix":
        return False
    os.chmod(path, mode)
    return True


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    try:
        restrict_permissions(temporary)
    except OSError:
        pass
    os.replace(temporary, path)


@dataclass(frozen=True)
class NodeIdentity:
    """Stable node identifier backed by a locally stored secret."""

    node_id: str
    secret: bytes

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.secret).hexdigest()


class NodeIdentityStore:
    """Create or load one stable identity without exposing its secret."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load_or_create(self) -> NodeIdentity:
        if self.path.exists():
            try:
                record = json.loads(self.path.read_text())
                secret = bytes.fromhex(record["secret"])
                node_id = record["node_id"]
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise IdentityError(f"invalid node identity: {self.path}") from exc
            if not node_id or len(secret) < 32:
                raise IdentityError(f"invalid node identity: {self.path}")
            expected = self._node_id(secret)
            if node_id != expected:
                raise IdentityError("node identity fingerprint does not match node ID")
            return NodeIdentity(node_id, secret)

        secret = secrets.token_bytes(32)
        identity = NodeIdentity(self._node_id(secret), secret)
        _write_private_json(
            self.path,
            {"node_id": identity.node_id, "secret": identity.secret.hex()},
        )
        return identity

    @staticmethod
    def _node_id(secret: bytes) -> str:
        return f"node-{hashlib.sha256(secret).hexdigest()[:32]}"


@dataclass(frozen=True)
class PairingCode:
    """Short-lived one-time pairing code returned only to the local operator."""

    token_id: str
    code: str
    expires_at: float


class PairingCodeStore:
    """Persist only hashes of pairing codes and consume them once."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def issue(self, ttl: float = 300.0) -> PairingCode:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        token_id = secrets.token_urlsafe(18)
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = self.clock() + ttl
        record = {
            "token_id": token_id,
            "code_hash": self._hash(token_id, code),
            "expires_at": expires_at,
        }
        _write_private_json(self.path, record)
        return PairingCode(token_id, code, expires_at)

    def consume(self, token_id: str, code: str) -> bool:
        try:
            record = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        if record.get("token_id") != token_id:
            return False
        if self.clock() >= float(record.get("expires_at", 0)):
            return False
        if not secrets.compare_digest(
            record.get("code_hash", ""), self._hash(token_id, code)
        ):
            return False
        self.path.unlink(missing_ok=True)
        return True

    @staticmethod
    def _hash(token_id: str, code: str) -> str:
        return hashlib.sha256(f"{token_id}:{code}".encode()).hexdigest()


@dataclass(frozen=True)
class PeerRecord:
    node_id: str
    name: str
    fingerprint: str
    credential_ref: str
    revoked: bool = False
    paired_at: float = 0.0


class PairingStore:
    """Manage explicitly trusted peer identities and revocation."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._peers: dict[str, PeerRecord] = {}
        self._load()

    def trust(
        self,
        node_id: str,
        *,
        name: str,
        fingerprint: str,
        credential_ref: str,
    ) -> PeerRecord:
        if not all((node_id, name, fingerprint, credential_ref)):
            raise IdentityError("peer identity fields must not be empty")
        peer = PeerRecord(
            node_id=node_id,
            name=name,
            fingerprint=fingerprint,
            credential_ref=credential_ref,
            paired_at=self.clock(),
        )
        self._peers[node_id] = peer
        self._save()
        return peer

    def revoke(self, node_id: str) -> PeerRecord:
        peer = self.get(node_id)
        if peer is None:
            raise IdentityError(f"unknown peer: {node_id}")
        revoked = PeerRecord(**{**peer.__dict__, "revoked": True})
        self._peers[node_id] = revoked
        self._save()
        return revoked

    def remove(self, node_id: str) -> None:
        self._peers.pop(node_id, None)
        self._save()

    def get(self, node_id: str) -> Optional[PeerRecord]:
        return self._peers.get(node_id)

    def is_trusted(self, node_id: str, fingerprint: str) -> bool:
        peer = self.get(node_id)
        return bool(peer and not peer.revoked and peer.fingerprint == fingerprint)

    def list(self) -> list[PeerRecord]:
        return sorted(self._peers.values(), key=lambda peer: peer.node_id)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self._peers = {
                node_id: PeerRecord(**record)
                for node_id, record in data.get("peers", {}).items()
            }
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IdentityError(f"invalid pairing state: {self.path}") from exc

    def _save(self) -> None:
        _write_private_json(
            self.path,
            {"peers": {node_id: peer.__dict__ for node_id, peer in self._peers.items()}},
        )
