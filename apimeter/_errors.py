"""Exception hierarchy for the api-meter client.

The shape mirrors the sentinel errors in the Go client.  Callers catch
specific subclasses to distinguish "back off" from "give up" from
"silently degrade":

- ``PermitWait``        → soft rate-limit; sleep ``wait_for`` then retry
- ``MeterDown``         → transport error or 5xx with no fallback
- ``Unauthorized``      → caller not allocated for this workload
- ``BadRequest``        → malformed input — permanent caller bug
- ``KillSwitchOn``      → returned only from introspection helpers; the
                           client itself short-circuits to a synthetic
                           grant when ``DISABLE_PERMIT_SERVICE`` is on
"""

from __future__ import annotations

from datetime import timedelta


class PermitError(Exception):
    """Base class — catch this to handle any api-meter failure."""


class PermitWait(PermitError):
    """Soft deny: caller should sleep ``wait_for`` and retry.

    Raised on HTTP 429 from the meter *and* on exhausted local-fallback
    buckets (so caller logic for "back off and try again" is identical
    whether the meter is healthy or degraded).
    """

    def __init__(self, wait_for: timedelta, reason: str = "") -> None:
        self.wait_for = wait_for
        self.reason = reason or "rate-limited"
        super().__init__(f"apimeter: wait {wait_for} before retrying ({self.reason})")


class PermitDenied(PermitError):
    """Reserved for future hard-deny semantics.  Not currently emitted."""


class MeterDown(PermitError):
    """Transport error, 5xx, or decode failure, and no fallback was configured."""


class Unauthorized(PermitError):
    """Caller not authorized for this workload (401/403)."""


class BadRequest(PermitError):
    """Caller bug (400) — missing required field, invalid value, etc."""


class KillSwitchOn(PermitError):
    """Kill switch is active; used by introspection helpers (not raised by permit())."""
