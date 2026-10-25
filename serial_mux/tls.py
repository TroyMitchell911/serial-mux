"""TLS context construction for authenticated Agent connections."""

from __future__ import annotations

import ssl
from pathlib import Path


class TLSError(ValueError):
    """Raised when TLS configuration is incomplete or unsafe."""


def _require_file(path: Path, label: str) -> str:
    path = Path(path)
    if not path.is_file():
        raise TLSError(f"{label} does not exist: {path}")
    return str(path)


def create_server_context(
    *,
    certificate: Path,
    private_key: Path,
    ca_certificate: Path,
) -> ssl.SSLContext:
    """Create a TLS 1.3 server requiring a client certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=_require_file(ca_certificate, "CA certificate"))
    context.load_cert_chain(
        certfile=_require_file(certificate, "server certificate"),
        keyfile=_require_file(private_key, "server private key"),
    )
    return context


def create_client_context(
    *,
    ca_certificate: Path,
    certificate: Path,
    private_key: Path,
    server_name: str,
) -> ssl.SSLContext:
    """Create a TLS 1.3 client with mutual certificate authentication."""
    if not server_name:
        raise TLSError("server_name must not be empty")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=_require_file(ca_certificate, "CA certificate"))
    context.load_cert_chain(
        certfile=_require_file(certificate, "client certificate"),
        keyfile=_require_file(private_key, "client private key"),
    )
    return context
