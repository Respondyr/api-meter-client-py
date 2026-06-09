"""httpx client builder for the api-meter mTLS listener.

The api-meter main listener requires `tls.RequireAndVerifyClientCert`. The
consuming pod mounts its client cert, private key, and the api-meter CA at
known file paths (projected from SPC). This helper produces an httpx
client preconfigured so callers don't reimplement the cert wiring.

Rotation is handled by SPC + Reloader at the pod level — when the SM blob
changes, the pod restarts and re-reads the files. The helper does not
hot-reload.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx

__all__ = ["build_mtls_async_client", "build_mtls_httpx_client"]


def _build_ssl_context(cert_path: str, key_path: str, ca_path: str) -> ssl.SSLContext:
    cert = Path(cert_path)
    key = Path(key_path)
    ca = Path(ca_path)
    if not cert.is_file():
        raise FileNotFoundError(f"apimeter: client cert not found at {cert}")
    if not key.is_file():
        raise FileNotFoundError(f"apimeter: client key not found at {key}")
    if not ca.is_file():
        raise FileNotFoundError(f"apimeter: CA not found at {ca}")
    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def build_mtls_httpx_client(
    cert_path: str,
    key_path: str,
    ca_path: str,
    *,
    timeout: float = 5.0,
) -> httpx.Client:
    """Sync httpx.Client with mTLS verification of the api-meter server."""
    ctx = _build_ssl_context(cert_path, key_path, ca_path)
    return httpx.Client(verify=ctx, timeout=timeout)


def build_mtls_async_client(
    cert_path: str,
    key_path: str,
    ca_path: str,
    *,
    timeout: float = 5.0,
) -> httpx.AsyncClient:
    """Async httpx.AsyncClient with mTLS verification of the api-meter server."""
    ctx = _build_ssl_context(cert_path, key_path, ca_path)
    return httpx.AsyncClient(verify=ctx, timeout=timeout)
