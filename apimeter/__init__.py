"""Python client for the api-meter permit service.

Mirror of ``github.com/Respondyr/api-meter-client-go``.  Same behavior,
same three safety valves (local fallback, ``DISABLE_PERMIT_SERVICE`` env
kill switch, request timeout), same SPIFFE/mTLS expectations.

Both a synchronous ``Client`` and an async ``AsyncClient`` are provided;
they share the local-fallback bucket and kill-switch logic so behavior
is identical across sync/async callers.

Typical use::

    from apimeter import AsyncClient, LocalFallbackConfig, ReportInput
    from apimeter import PermitWait, MeterDown

    c = AsyncClient(
        base_url="http://api-meter.api-meter.svc.cluster.local:8080",
        caller="outreach",
        workload="outreach",
        local_fallback=LocalFallbackConfig(rate_per_second=0.5, burst=2),
    )

    try:
        permit = await c.permit("resend", "send-email")
    except PermitWait as w:
        await asyncio.sleep(w.wait_for.total_seconds())
        return
    except MeterDown:
        return  # caller decides: degrade gracefully

    # ... perform upstream call, record start/status/latency ...

    await c.report(ReportInput(
        request_id=permit.request_id,
        provider="resend",
        endpoint="send-email",
        status_code=resp.status_code,
        latency_ms=int(elapsed * 1000),
    ))
"""

from __future__ import annotations

from ._client import AsyncClient, Client
from ._errors import (
    BadRequest,
    KillSwitchOn,
    MeterDown,
    PermitDenied,
    PermitError,
    PermitWait,
    Unauthorized,
)
from ._models import LocalFallbackConfig, Permit, ReportInput

__all__ = [
    "AsyncClient",
    "BadRequest",
    "Client",
    "KillSwitchOn",
    "LocalFallbackConfig",
    "MeterDown",
    "Permit",
    "PermitDenied",
    "PermitError",
    "PermitWait",
    "ReportInput",
    "Unauthorized",
]

__version__ = "0.1.0"
