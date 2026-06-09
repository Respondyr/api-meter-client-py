"""Sync and async api-meter clients.

The two classes share the exact same wire protocol, safety valves, and
local-fallback bucket.  Everything specific to HTTP transport lives in
``_do_request``/``_do_request_async``; all the decision logic is in
``_handle_permit_response``/``_handle_report_response`` so the two
transports can't drift.

Design echoes of the Go client:

- ``DISABLE_PERMIT_SERVICE`` short-circuits to a synthetic grant (no
  request_id → report() silently no-ops → counter gap visible in
  dashboards).
- On transport error, 5xx, or decode failure the client falls through
  to the in-process bucket when ``local_fallback`` is set, else raises
  ``MeterDown``.
- Reports are best-effort.  A dropped report is a dashboard fidelity
  loss, not a correctness loss — the upstream already happened.
- The ``X-Caller-SPIFFE-ID`` header is always set so dev-mode listeners
  (mTLS disabled) can still pin the caller identity.  Production
  listeners ignore the header and trust the peer cert.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any

import httpx

from ._bucket import LocalBucket
from ._errors import BadRequest, MeterDown, PermitWait, Unauthorized
from ._killswitch import is_kill_switch_on
from ._models import LocalFallbackConfig, Permit, ReportInput

log = logging.getLogger("apimeter")

_DEFAULT_TIMEOUT = 0.150  # seconds; match the Go client
_VALID_WORKLOADS = frozenset({"platform", "outreach"})

try:
    from prometheus_client import Counter

    _LOCAL_FALLBACK_GRANTS = Counter(
        "apimeter_local_fallback_grants_total",
        (
            "Permits granted by in-process LocalFallback because meter was "
            "unreachable. Every increment is a silent bypass — paid calls "
            "proceeding without spend tracking or rate limit. Alert on rate>0."
        ),
        labelnames=("caller", "provider", "endpoint"),
    )
except ImportError:  # pragma: no cover — keep client usable without prometheus_client
    _LOCAL_FALLBACK_GRANTS = None


def _build_permit_body(provider: str, endpoint: str, caller: str, workload: str) -> dict:
    return {
        "provider": provider,
        "endpoint": endpoint,
        "caller": caller,
        "workload": workload,
    }


def _build_report_body(inp: ReportInput, workload: str) -> dict:
    body: dict[str, Any] = {
        "request_id": inp.request_id,
        "provider": inp.provider,
        "endpoint": inp.endpoint,
        "workload": workload,
        "latency_ms": inp.latency_ms,
    }
    if inp.status_code is not None:
        body["status_code"] = inp.status_code
    if inp.transport_error:
        body["transport_error"] = inp.transport_error
    if inp.retry_after_seconds is not None:
        body["retry_after_seconds"] = inp.retry_after_seconds
    if inp.rate_limit_remaining is not None:
        body["rate_limit_remaining"] = inp.rate_limit_remaining
    return body


def _headers(caller: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Dev-mode identity hint; prod listeners trust the peer cert.
        "X-Caller-SPIFFE-ID": f"spiffe://respondyr/{caller}",
    }


def _parse_wait(resp: httpx.Response) -> timedelta:
    """Read ``Retry-After-Ms`` header then fall back to body ``wait_ms``.

    Defaults to 1 second if neither is set — callers that see a
    ``PermitWait`` with zero sleep tend to hot-loop, which is worse than
    a slightly-too-long sleep on a misbehaving meter.
    """
    hdr = resp.headers.get("Retry-After-Ms")
    if hdr:
        try:
            ms = int(hdr)
            if ms > 0:
                return timedelta(milliseconds=ms)
        except ValueError:
            pass
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return timedelta(seconds=1)
    wait_ms = body.get("wait_ms") if isinstance(body, dict) else None
    if isinstance(wait_ms, int) and wait_ms > 0:
        return timedelta(milliseconds=wait_ms)
    return timedelta(seconds=1)


class _Base:
    """Shared init + fallback logic for sync and async clients."""

    def __init__(
        self,
        *,
        base_url: str,
        caller: str,
        workload: str,
        request_timeout: float | None = None,
        local_fallback: LocalFallbackConfig | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("apimeter: base_url is required")
        if not caller:
            raise ValueError("apimeter: caller is required")
        if workload not in _VALID_WORKLOADS:
            raise ValueError(
                f"apimeter: workload must be 'platform' or 'outreach', got {workload!r}"
            )

        self._base_url = base_url.rstrip("/")
        self._caller = caller
        self._workload = workload
        self._timeout = (
            request_timeout if request_timeout and request_timeout > 0 else _DEFAULT_TIMEOUT
        )
        self._fallback: LocalBucket | None = (
            LocalBucket(local_fallback) if local_fallback is not None else None
        )

    # -- decision helpers ------------------------------------------------

    def _killswitch_permit(self) -> Permit:
        return Permit(granted=True, request_id="", source="killswitch")

    def _fallback_permit(self, provider: str, endpoint: str, cause: Exception) -> Permit:
        """Consult the in-process bucket, or re-raise as ``MeterDown``."""
        if self._fallback is None:
            raise MeterDown(f"apimeter: meter unreachable: {cause}") from cause

        if self._fallback.take(provider, endpoint):
            if _LOCAL_FALLBACK_GRANTS is not None:
                _LOCAL_FALLBACK_GRANTS.labels(
                    caller=self._caller, provider=provider, endpoint=endpoint
                ).inc()
            log.warning(
                "apimeter: local fallback granted permit — meter unreachable, "
                "call proceeding UNMETERED",
                extra={
                    "caller": self._caller,
                    "provider": provider,
                    "endpoint": endpoint,
                    "cause": str(cause),
                },
            )
            return Permit(
                granted=True,
                request_id="",
                reason="local-fallback",
                source="fallback",
            )
        # Bucket empty — treat as soft deny with the time until next refill.
        raise PermitWait(
            wait_for=self._fallback.wait_for(provider, endpoint),
            reason="local-fallback-empty",
        )

    def _interpret_permit(self, resp: httpx.Response, provider: str, endpoint: str) -> Permit:
        """Branch on status code.  Sync and async share this."""
        status = resp.status_code
        if status == 200:
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                return self._fallback_permit(provider, endpoint, exc)
            return Permit(
                granted=bool(data.get("granted", False)),
                request_id=data.get("request_id", ""),
                reason=data.get("reason", ""),
                source="meter",
            )
        if status == 429:
            wait = _parse_wait(resp)
            raise PermitWait(wait_for=wait)
        if status == 400:
            raise BadRequest(f"apimeter: {resp.text[:512]}")
        if status in (401, 403):
            raise Unauthorized(f"apimeter: {resp.text[:512]}")
        # 5xx or anything else — treat as meter trouble, fall through.
        return self._fallback_permit(
            provider,
            endpoint,
            RuntimeError(f"unexpected status {status}: {resp.text[:256]}"),
        )

    def _interpret_report(self, resp: httpx.Response) -> None:
        if resp.status_code == 202:
            return
        if resp.status_code == 400:
            raise BadRequest(f"apimeter: report rejected: {resp.text[:512]}")
        raise MeterDown(f"apimeter: report status {resp.status_code}: {resp.text[:256]}")

    # -- introspection ---------------------------------------------------

    @property
    def caller(self) -> str:
        return self._caller

    @property
    def workload(self) -> str:
        return self._workload


class Client(_Base):
    """Synchronous client.  Use from thread-pool or straight-line code."""

    def __init__(
        self,
        *,
        base_url: str,
        caller: str,
        workload: str,
        request_timeout: float | None = None,
        local_fallback: LocalFallbackConfig | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            caller=caller,
            workload=workload,
            request_timeout=request_timeout,
            local_fallback=local_fallback,
        )
        self._http = http_client or httpx.Client(timeout=self._timeout)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- public ----------------------------------------------------------

    def permit(self, provider: str, endpoint: str) -> Permit:
        """Ask for permission to call ``(provider, endpoint)``.

        Returns a granted ``Permit`` on success.  Raises ``PermitWait``
        on soft deny, ``Unauthorized`` / ``BadRequest`` on hard deny, and
        ``MeterDown`` on unreachable meter with no fallback configured.
        """
        if is_kill_switch_on():
            return self._killswitch_permit()
        body = _build_permit_body(provider, endpoint, self._caller, self._workload)
        try:
            resp = self._http.post(
                f"{self._base_url}/v1/permit",
                json=body,
                headers=_headers(self._caller),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return self._fallback_permit(provider, endpoint, exc)
        return self._interpret_permit(resp, provider, endpoint)

    def report(self, inp: ReportInput) -> None:
        """Best-effort outcome report.  No-ops when ``request_id`` is empty."""
        if not inp.request_id:
            return
        if not inp.provider or not inp.endpoint:
            raise BadRequest("apimeter: provider and endpoint required")

        body = _build_report_body(inp, self._workload)
        try:
            resp = self._http.post(
                f"{self._base_url}/v1/report",
                json=body,
                headers=_headers(self._caller),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise MeterDown(f"apimeter: report failed: {exc}") from exc
        self._interpret_report(resp)


class AsyncClient(_Base):
    """Async (httpx.AsyncClient-backed) client.  Use from asyncio code."""

    def __init__(
        self,
        *,
        base_url: str,
        caller: str,
        workload: str,
        request_timeout: float | None = None,
        local_fallback: LocalFallbackConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            caller=caller,
            workload=workload,
            request_timeout=request_timeout,
            local_fallback=local_fallback,
        )
        self._http = http_client or httpx.AsyncClient(timeout=self._timeout)
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def permit(self, provider: str, endpoint: str) -> Permit:
        if is_kill_switch_on():
            return self._killswitch_permit()
        body = _build_permit_body(provider, endpoint, self._caller, self._workload)
        try:
            resp = await self._http.post(
                f"{self._base_url}/v1/permit",
                json=body,
                headers=_headers(self._caller),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return self._fallback_permit(provider, endpoint, exc)
        return self._interpret_permit(resp, provider, endpoint)

    async def report(self, inp: ReportInput) -> None:
        if not inp.request_id:
            return
        if not inp.provider or not inp.endpoint:
            raise BadRequest("apimeter: provider and endpoint required")

        body = _build_report_body(inp, self._workload)
        try:
            resp = await self._http.post(
                f"{self._base_url}/v1/report",
                json=body,
                headers=_headers(self._caller),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise MeterDown(f"apimeter: report failed: {exc}") from exc
        self._interpret_report(resp)


# -- convenience: let callers serialize ReportInput via asdict() -------


def _report_asdict(inp: ReportInput) -> dict:
    """Used by tests; consumers shouldn't need this."""
    return asdict(inp)
