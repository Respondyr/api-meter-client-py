"""Async ``AsyncClient`` tests.

We re-test the branches that differ in transport (sync vs async) —
the shared logic is already exercised in ``test_sync_client.py``.  The
goal is to guarantee the two transports don't drift on behavior.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from apimeter import (
    AsyncClient,
    BadRequest,
    LocalFallbackConfig,
    MeterDown,
    PermitWait,
    ReportInput,
    Unauthorized,
)

BASE = "http://api-meter.local:8080"


def _new(**overrides) -> AsyncClient:
    kwargs = {
        "base_url": BASE,
        "caller": "outreach",
        "workload": "outreach",
        "request_timeout": 0.5,
    }
    kwargs.update(overrides)
    return AsyncClient(**kwargs)


@respx.mock
async def test_async_permit_200():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(
            200,
            json={"granted": True, "request_id": "req-async-1"},
        )
    )
    async with _new() as c:
        p = await c.permit("resend", "send-email")
    assert p.granted is True
    assert p.request_id == "req-async-1"
    assert p.source == "meter"


@respx.mock
async def test_async_permit_429_raises_permit_wait():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(429, headers={"Retry-After-Ms": "500"})
    )
    async with _new() as c:
        with pytest.raises(PermitWait) as exc_info:
            await c.permit("resend", "send-email")
    assert exc_info.value.wait_for.total_seconds() == 0.5


@respx.mock
async def test_async_permit_401_raises_unauthorized():
    respx.post(f"{BASE}/v1/permit").mock(return_value=httpx.Response(401, text="x"))
    async with _new() as c:
        with pytest.raises(Unauthorized):
            await c.permit("resend", "send-email")


@respx.mock
async def test_async_permit_transport_error_no_fallback():
    respx.post(f"{BASE}/v1/permit").mock(side_effect=httpx.ConnectError("boom"))
    async with _new() as c:
        with pytest.raises(MeterDown):
            await c.permit("resend", "send-email")


@respx.mock
async def test_async_permit_transport_error_with_fallback():
    respx.post(f"{BASE}/v1/permit").mock(side_effect=httpx.ConnectError("boom"))
    async with _new(local_fallback=LocalFallbackConfig(rate_per_second=10, burst=2)) as c:
        p = await c.permit("resend", "send-email")
    assert p.granted is True
    assert p.source == "fallback"


async def test_async_kill_switch(monkeypatch):
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", "1")
    async with _new() as c:
        p = await c.permit("resend", "send-email")
    assert p.source == "killswitch"
    assert p.granted is True


@respx.mock
async def test_async_report_202():
    respx.post(f"{BASE}/v1/report").mock(return_value=httpx.Response(202))
    async with _new() as c:
        await c.report(
            ReportInput(
                request_id="r1",
                provider="resend",
                endpoint="send-email",
                status_code=200,
                latency_ms=42,
            )
        )


async def test_async_report_empty_request_id_noops():
    # No mock — proves we never hit the wire.
    async with _new() as c:
        await c.report(
            ReportInput(
                request_id="",
                provider="resend",
                endpoint="send-email",
                latency_ms=1,
            )
        )


@respx.mock
async def test_async_report_400_bad_request():
    respx.post(f"{BASE}/v1/report").mock(
        return_value=httpx.Response(400, text="missing field")
    )
    async with _new() as c:
        with pytest.raises(BadRequest):
            await c.report(
                ReportInput(
                    request_id="r1",
                    provider="resend",
                    endpoint="send-email",
                    status_code=200,
                    latency_ms=1,
                )
            )


@respx.mock
async def test_async_report_network_error_raises_meter_down():
    respx.post(f"{BASE}/v1/report").mock(side_effect=httpx.ConnectError("x"))
    async with _new() as c:
        with pytest.raises(MeterDown):
            await c.report(
                ReportInput(
                    request_id="r1",
                    provider="resend",
                    endpoint="send-email",
                    status_code=200,
                    latency_ms=1,
                )
            )
