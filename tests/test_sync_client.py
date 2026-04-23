"""Sync ``Client`` behavior tests (httpx mocked via respx).

Covers the happy path, every documented error branch, the safety valves
(local fallback, kill switch), and the report lifecycle.  The async
client shares 90% of its implementation with the sync one; we re-test
the same branches against it in ``test_async_client.py`` rather than
re-test plain internals.
"""

from __future__ import annotations

import json as _j

import httpx
import pytest
import respx

from apimeter import (
    BadRequest,
    Client,
    LocalFallbackConfig,
    MeterDown,
    PermitWait,
    ReportInput,
    Unauthorized,
)

BASE = "http://api-meter.local:8080"


def _new(**overrides) -> Client:
    kwargs = {
        "base_url": BASE,
        "caller": "reviews",
        "workload": "platform",
        "request_timeout": 0.5,
    }
    kwargs.update(overrides)
    return Client(**kwargs)


# ---- construction ------------------------------------------------------


def test_rejects_empty_base_url():
    with pytest.raises(ValueError, match="base_url"):
        Client(base_url="", caller="x", workload="platform")


def test_rejects_empty_caller():
    with pytest.raises(ValueError, match="caller"):
        Client(base_url=BASE, caller="", workload="platform")


def test_rejects_bogus_workload():
    with pytest.raises(ValueError, match="workload"):
        Client(base_url=BASE, caller="reviews", workload="banana")


def test_accepts_outreach_workload():
    c = Client(base_url=BASE, caller="outreach", workload="outreach")
    assert c.workload == "outreach"


# ---- permit happy path -------------------------------------------------


@respx.mock
def test_permit_200_grants():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(
            200,
            json={"granted": True, "request_id": "req-123"},
        )
    )
    with _new() as c:
        p = c.permit("gbp", "reply-review")
    assert p.granted is True
    assert p.request_id == "req-123"
    assert p.source == "meter"


@respx.mock
def test_permit_sends_caller_and_workload_in_body():
    route = respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(200, json={"granted": True, "request_id": "x"})
    )
    with _new() as c:
        c.permit("gbp", "reply-review")
    sent = route.calls.last.request
    assert sent.headers["x-caller-spiffe-id"] == "spiffe://undercurrent/reviews"

    body = _j.loads(sent.content)
    assert body == {
        "provider": "gbp",
        "endpoint": "reply-review",
        "caller": "reviews",
        "workload": "platform",
    }


# ---- permit error branches --------------------------------------------


@respx.mock
def test_permit_429_raises_permit_wait_with_header():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After-Ms": "750"},
            json={"granted": False, "wait_ms": 9999},  # header wins
        )
    )
    with _new() as c, pytest.raises(PermitWait) as exc_info:
        c.permit("gbp", "reply-review")
    assert exc_info.value.wait_for.total_seconds() == 0.75


@respx.mock
def test_permit_429_falls_back_to_body_wait_ms():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(
            429,
            json={"granted": False, "wait_ms": 1250},
        )
    )
    with _new() as c, pytest.raises(PermitWait) as exc_info:
        c.permit("gbp", "reply-review")
    assert exc_info.value.wait_for.total_seconds() == 1.25


@respx.mock
def test_permit_429_defaults_to_1s_when_empty():
    respx.post(f"{BASE}/v1/permit").mock(return_value=httpx.Response(429, json={}))
    with _new() as c, pytest.raises(PermitWait) as exc_info:
        c.permit("gbp", "reply-review")
    assert exc_info.value.wait_for.total_seconds() == 1.0


@respx.mock
def test_permit_400_raises_bad_request():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(400, text="missing provider")
    )
    with _new() as c, pytest.raises(BadRequest, match="missing provider"):
        c.permit("gbp", "reply-review")


@respx.mock
def test_permit_401_raises_unauthorized():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(401, text="not allocated")
    )
    with _new() as c, pytest.raises(Unauthorized, match="not allocated"):
        c.permit("gbp", "reply-review")


@respx.mock
def test_permit_403_raises_unauthorized():
    respx.post(f"{BASE}/v1/permit").mock(return_value=httpx.Response(403, text="no"))
    with _new() as c, pytest.raises(Unauthorized):
        c.permit("gbp", "reply-review")


# ---- transport failure + fallback ------------------------------------


@respx.mock
def test_permit_transport_error_no_fallback_raises_meter_down():
    respx.post(f"{BASE}/v1/permit").mock(side_effect=httpx.ConnectError("boom"))
    with _new() as c, pytest.raises(MeterDown):
        c.permit("gbp", "reply-review")


@respx.mock
def test_permit_transport_error_with_fallback_grants():
    respx.post(f"{BASE}/v1/permit").mock(side_effect=httpx.ConnectError("boom"))
    with _new(local_fallback=LocalFallbackConfig(rate_per_second=10, burst=2)) as c:
        p = c.permit("gbp", "reply-review")
    assert p.granted is True
    assert p.source == "fallback"
    assert p.request_id == ""


@respx.mock
def test_permit_5xx_with_fallback_grants():
    respx.post(f"{BASE}/v1/permit").mock(return_value=httpx.Response(503, text="down"))
    with _new(local_fallback=LocalFallbackConfig(rate_per_second=10, burst=2)) as c:
        p = c.permit("gbp", "reply-review")
    assert p.granted is True
    assert p.source == "fallback"


@respx.mock
def test_permit_5xx_no_fallback_raises():
    respx.post(f"{BASE}/v1/permit").mock(return_value=httpx.Response(500, text="oops"))
    with _new() as c, pytest.raises(MeterDown):
        c.permit("gbp", "reply-review")


@respx.mock
def test_permit_garbage_json_falls_through_to_fallback():
    respx.post(f"{BASE}/v1/permit").mock(
        return_value=httpx.Response(200, content=b"not json")
    )
    with _new(local_fallback=LocalFallbackConfig(rate_per_second=10, burst=2)) as c:
        p = c.permit("gbp", "reply-review")
    assert p.granted is True
    assert p.source == "fallback"


@respx.mock
def test_permit_fallback_bucket_empty_raises_permit_wait():
    respx.post(f"{BASE}/v1/permit").mock(side_effect=httpx.ConnectError("boom"))
    with _new(local_fallback=LocalFallbackConfig(rate_per_second=0.1, burst=1)) as c:
        c.permit("gbp", "reply-review")  # drains the one token
        with pytest.raises(PermitWait) as exc_info:
            c.permit("gbp", "reply-review")
        assert exc_info.value.reason == "local-fallback-empty"


# ---- kill switch -------------------------------------------------------


def test_kill_switch_short_circuits(monkeypatch):
    # No HTTP mocked — if the client actually hit the wire, httpx would
    # raise. The kill switch must prevent that entirely.
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", "true")
    with _new() as c:
        p = c.permit("gbp", "reply-review")
    assert p.granted is True
    assert p.source == "killswitch"
    assert p.request_id == ""


def test_kill_switch_is_read_per_call(monkeypatch):
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", "true")
    with _new() as c:
        p1 = c.permit("gbp", "reply-review")
        assert p1.source == "killswitch"
        monkeypatch.setenv("DISABLE_PERMIT_SERVICE", "false")
        # Now the client should try to call — respx without a route will
        # 404 on httpx's side.  We don't care about the failure mode;
        # we care that the kill switch no longer short-circuits.
        with respx.mock(assert_all_called=False) as rx:
            rx.post(f"{BASE}/v1/permit").mock(
                return_value=httpx.Response(200, json={"granted": True, "request_id": "r"})
            )
            p2 = c.permit("gbp", "reply-review")
        assert p2.source == "meter"


# ---- report ------------------------------------------------------------


@respx.mock
def test_report_202_ok():
    respx.post(f"{BASE}/v1/report").mock(return_value=httpx.Response(202))
    with _new() as c:
        c.report(
            ReportInput(
                request_id="r1",
                provider="gbp",
                endpoint="reply-review",
                status_code=200,
                latency_ms=120,
            )
        )


@respx.mock
def test_report_wire_shape_includes_workload_and_status_code():
    route = respx.post(f"{BASE}/v1/report").mock(return_value=httpx.Response(202))
    with _new() as c:
        c.report(
            ReportInput(
                request_id="r1",
                provider="gbp",
                endpoint="reply-review",
                status_code=429,
                latency_ms=777,
                retry_after_seconds=30,
                rate_limit_remaining=5,
            )
        )

    body = _j.loads(route.calls.last.request.content)
    assert body == {
        "request_id": "r1",
        "provider": "gbp",
        "endpoint": "reply-review",
        "workload": "platform",
        "latency_ms": 777,
        "status_code": 429,
        "retry_after_seconds": 30,
        "rate_limit_remaining": 5,
    }


@respx.mock
def test_report_with_transport_error_omits_status_code():
    route = respx.post(f"{BASE}/v1/report").mock(return_value=httpx.Response(202))
    with _new() as c:
        c.report(
            ReportInput(
                request_id="r1",
                provider="gbp",
                endpoint="reply-review",
                transport_error="connection reset",
                latency_ms=42,
            )
        )

    body = _j.loads(route.calls.last.request.content)
    assert "status_code" not in body
    assert body["transport_error"] == "connection reset"


def test_report_empty_request_id_noops(monkeypatch):
    # Must not hit the wire at all — if it did, respx wouldn't have a
    # route and httpx would error.  Proven by skipping the mock entirely.
    with _new() as c:
        c.report(
            ReportInput(
                request_id="",
                provider="gbp",
                endpoint="reply-review",
                status_code=200,
                latency_ms=100,
            )
        )


def test_report_missing_provider_raises_bad_request():
    with _new() as c, pytest.raises(BadRequest):
        c.report(
            ReportInput(
                request_id="r1",
                provider="",
                endpoint="reply-review",
                latency_ms=1,
            )
        )


@respx.mock
def test_report_400_raises_bad_request():
    respx.post(f"{BASE}/v1/report").mock(
        return_value=httpx.Response(400, text="bad field")
    )
    with _new() as c, pytest.raises(BadRequest, match="bad field"):
        c.report(
            ReportInput(
                request_id="r1",
                provider="gbp",
                endpoint="reply-review",
                status_code=200,
                latency_ms=1,
            )
        )


@respx.mock
def test_report_network_error_raises_meter_down():
    respx.post(f"{BASE}/v1/report").mock(side_effect=httpx.ConnectError("down"))
    with _new() as c, pytest.raises(MeterDown):
        c.report(
            ReportInput(
                request_id="r1",
                provider="gbp",
                endpoint="reply-review",
                status_code=200,
                latency_ms=1,
            )
        )


@respx.mock
def test_report_500_raises_meter_down():
    respx.post(f"{BASE}/v1/report").mock(return_value=httpx.Response(500, text="x"))
    with _new() as c, pytest.raises(MeterDown):
        c.report(
            ReportInput(
                request_id="r1",
                provider="gbp",
                endpoint="reply-review",
                status_code=200,
                latency_ms=1,
            )
        )
