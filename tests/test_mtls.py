"""Tests for the mTLS httpx-client helpers and the LocalFallback counter."""

from __future__ import annotations

import datetime

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from apimeter import build_mtls_async_client, build_mtls_httpx_client


def _self_signed(common_name: str) -> tuple[bytes, bytes]:
    """Return (cert_pem, key_pem) for a throwaway self-signed cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    # Fixed dates — Date.now() equivalents are fine in a test process.
    not_before = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    not_after = datetime.datetime(2040, 1, 1, tzinfo=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


@pytest.fixture
def cert_files(tmp_path):
    """Write a client cert/key + a CA cert to disk; return their paths."""
    client_cert, client_key = _self_signed("test-client")
    ca_cert, _ = _self_signed("test-ca")
    cert_path = tmp_path / "client.crt"
    key_path = tmp_path / "client.key"
    ca_path = tmp_path / "ca.pem"
    cert_path.write_bytes(client_cert)
    key_path.write_bytes(client_key)
    ca_path.write_bytes(ca_cert)
    return str(cert_path), str(key_path), str(ca_path)


def test_build_mtls_httpx_client_returns_sync_client(cert_files):
    cert, key, ca = cert_files
    client = build_mtls_httpx_client(cert, key, ca)
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


@pytest.mark.asyncio
async def test_build_mtls_async_client_returns_async_client(cert_files):
    cert, key, ca = cert_files
    client = build_mtls_async_client(cert, key, ca)
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


def test_missing_cert_raises(cert_files, tmp_path):
    _, key, ca = cert_files
    with pytest.raises(FileNotFoundError, match="client cert not found"):
        build_mtls_httpx_client(str(tmp_path / "nope.crt"), key, ca)


def test_missing_key_raises(cert_files, tmp_path):
    cert, _, ca = cert_files
    with pytest.raises(FileNotFoundError, match="client key not found"):
        build_mtls_httpx_client(cert, str(tmp_path / "nope.key"), ca)


def test_missing_ca_raises(cert_files, tmp_path):
    cert, key, _ = cert_files
    with pytest.raises(FileNotFoundError, match="CA not found"):
        build_mtls_httpx_client(cert, key, str(tmp_path / "nope.pem"))


def test_local_fallback_grant_increments_counter():
    """A fallback-granted permit bumps apimeter_local_fallback_grants_total."""
    prometheus_client = pytest.importorskip("prometheus_client")
    from apimeter import Client, LocalFallbackConfig

    labels = {"caller": "reviews", "provider": "gbp", "endpoint": "list-reviews"}
    before = (
        prometheus_client.REGISTRY.get_sample_value("apimeter_local_fallback_grants_total", labels)
        or 0.0
    )

    # base_url points nowhere reachable; the transport error drives the
    # client into _fallback_permit, which grants from the in-process bucket.
    client = Client(
        base_url="http://127.0.0.1:1",
        caller="reviews",
        workload="platform",
        local_fallback=LocalFallbackConfig(rate_per_second=1.0, burst=3),
    )
    try:
        permit = client.permit("gbp", "list-reviews")
    finally:
        client.close()

    assert permit.source == "fallback"
    assert permit.granted is True
    after = prometheus_client.REGISTRY.get_sample_value(
        "apimeter_local_fallback_grants_total", labels
    )
    assert after == before + 1.0
