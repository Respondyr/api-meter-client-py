# api-meter-client-py

Python client for the [api-meter](https://github.com/Respondyr/api-meter) permit service.
Mirror of [api-meter-client-go](https://github.com/Respondyr/api-meter-client-go) — same
wire protocol, same safety valves, same behavior.

## Install

```
pip install git+https://github.com/Respondyr/api-meter-client-py.git@v0.1.0
```

Or, add to `pyproject.toml`:

```toml
dependencies = [
    "api-meter-client @ git+https://github.com/Respondyr/api-meter-client-py.git@v0.1.0",
]
```

## Usage — async

```python
from apimeter import AsyncClient, LocalFallbackConfig, ReportInput, PermitWait, MeterDown

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
    return  # caller decides how to degrade

start = time.monotonic()
try:
    resp = await httpx_client.post(...)
    await c.report(ReportInput(
        request_id=permit.request_id,
        provider="resend",
        endpoint="send-email",
        status_code=resp.status_code,
        latency_ms=int((time.monotonic() - start) * 1000),
    ))
except httpx.HTTPError as exc:
    await c.report(ReportInput(
        request_id=permit.request_id,
        provider="resend",
        endpoint="send-email",
        transport_error=str(exc),
        latency_ms=int((time.monotonic() - start) * 1000),
    ))
```

## Usage — sync

```python
from apimeter import Client, LocalFallbackConfig, ReportInput, PermitWait, MeterDown

with Client(base_url=..., caller="ai", workload="platform") as c:
    try:
        permit = c.permit("anthropic", "messages")
    except PermitWait as w:
        time.sleep(w.wait_for.total_seconds())
        return
    # ... upstream call ...
    c.report(ReportInput(request_id=permit.request_id, ...))
```

## Safety valves

Three layers protect callers when the meter is briefly unreachable:

| Layer | What it does | When it fires |
|---|---|---|
| `request_timeout` (default 150ms) | Caps per-call duration | Slow meter; prevents the permit hop from dominating caller latency |
| `local_fallback` | In-process token bucket with pessimistic rate | HTTP error, 5xx, decode error |
| `DISABLE_PERMIT_SERVICE` env | Short-circuits to always-grant | Operator breakglass — set on caller pod to bypass the meter during an incident |

`request_id` is empty on fallback / kill-switch grants. `report()` silently no-ops on empty
ids, so dashboards can detect degraded periods via the `grants > 0 && reports == 0` counter
gap.

## Exceptions

All failures subclass `PermitError`:

| Exception | Meaning |
|---|---|
| `PermitWait(wait_for, reason)` | Soft rate-limit; sleep `wait_for` and retry |
| `MeterDown` | Transport error or 5xx with no fallback configured |
| `Unauthorized` | Caller not allocated for this workload (401/403) |
| `BadRequest` | Caller bug — missing field, invalid value, etc. (400) |

## Dev

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

## License

MIT.
