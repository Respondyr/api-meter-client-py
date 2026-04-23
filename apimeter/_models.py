"""Data classes for permit + report inputs/outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

Source = Literal["meter", "fallback", "killswitch"]


@dataclass(frozen=True)
class Permit:
    """The meter's decision.

    On a normal grant (``source="meter"``), ``request_id`` is non-empty
    and must be passed to ``Report`` so the outcome is correlated on
    the meter side.  On fallback / kill-switch grants the id is empty,
    and ``Report`` silently no-ops — that gap is the observable signal
    for "we're running degraded."
    """

    granted: bool
    request_id: str = ""
    wait_for: timedelta = timedelta()
    reason: str = ""
    source: Source = "meter"


@dataclass
class ReportInput:
    """Superset of fields accepted by ``POST /v1/report``.

    Exactly one of ``status_code`` / ``transport_error`` must be set —
    the meter returns 400 otherwise.  ``retry_after_seconds`` and
    ``rate_limit_remaining`` are both optional pass-throughs from the
    upstream response headers when available.
    """

    request_id: str
    provider: str
    endpoint: str
    status_code: int | None = None
    transport_error: str = ""
    latency_ms: int = 0
    retry_after_seconds: int | None = None
    rate_limit_remaining: int | None = None


@dataclass(frozen=True)
class LocalFallbackConfig:
    """Pessimistic in-process rate used when the meter is unreachable.

    ``rate_per_second`` is the steady-state refill.  ``burst`` is the
    bucket capacity (max tokens held at rest).  Tune these *below* the
    true upstream ceiling so a caller stuck on fallback can't outpace
    what the meter would have granted anyway.
    """

    rate_per_second: float = 1.0
    burst: int = 5
