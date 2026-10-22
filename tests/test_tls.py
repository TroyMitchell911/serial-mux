"""Tests for TLS context configuration."""

import ssl

import pytest

from serial_mux.tls import TLSError, create_client_context, create_server_context


def test_tls_context_requires_existing_credentials(tmp_path):
    with pytest.raises(TLSError, match="CA certificate"):
        create_server_context(
            certificate=tmp_path / "server.crt",
            private_key=tmp_path / "server.key",
            ca_certificate=tmp_path / "ca.crt",
        )


def test_client_context_requires_server_name(tmp_path):
    ca = tmp_path / "ca.crt"
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    for path in (ca, cert, key):
        path.write_text("placeholder")

    with pytest.raises(TLSError, match="server_name"):
        create_client_context(
            ca_certificate=ca,
            certificate=cert,
            private_key=key,
            server_name="",
        )


def test_tls_minimum_version_is_tls_13(monkeypatch, tmp_path):
    class FakeContext:
        def __init__(self, protocol):
            self.protocol = protocol
            self.minimum_version = None
            self.verify_mode = None
            self.check_hostname = None

        def load_verify_locations(self, cafile):
            self.cafile = cafile

        def load_cert_chain(self, certfile, keyfile):
            self.certfile = certfile
            self.keyfile = keyfile

    monkeypatch.setattr(ssl, "SSLContext", FakeContext)
    ca = tmp_path / "ca.crt"
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    for path in (ca, cert, key):
        path.write_text("placeholder")

    context = create_server_context(
        certificate=cert,
        private_key=key,
        ca_certificate=ca,
    )

    assert context.protocol == ssl.PROTOCOL_TLS_SERVER
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_REQUIRED
