"""In-process token bucket for local fallback.

Keyed by ``(provider, endpoint)`` so a degraded bucket for one upstream
doesn't starve grants for another.  Thread-safe via a ``threading.Lock``
— the same bucket is shared by both sync and async client instances, so
the lock has to be OS-level, not ``asyncio.Lock``.
"""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from ._models import LocalFallbackConfig


@dataclass
class _BucketState:
    tokens: float
    last: float  # monotonic seconds, not wall-clock


class LocalBucket:
    """Tiny ``(provider, endpoint) -> token bucket`` map.

    Used only on fallback paths.  ``rate`` and ``capacity`` come from
    ``LocalFallbackConfig``; both are clamped to safe defaults so a
    zero/negative config still behaves sanely rather than deadlocking.
    """

    def __init__(
        self,
        cfg: LocalFallbackConfig,
        clock: Callable[[], float] | None = None,
    ) -> None:
        rate = cfg.rate_per_second if cfg.rate_per_second > 0 else 1.0
        cap = float(cfg.burst) if cfg.burst > 0 else 5.0
        self._rate = rate
        self._cap = cap
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._state: dict[str, _BucketState] = {}

    @staticmethod
    def _key(provider: str, endpoint: str) -> str:
        return f"{provider}|{endpoint}"

    def take(self, provider: str, endpoint: str) -> bool:
        """Attempt to draw one token; return ``True`` on success."""
        with self._lock:
            key = self._key(provider, endpoint)
            now = self._clock()
            state = self._state.get(key)
            if state is None:
                # First time we see this endpoint — start full so the
                # caller gets a few free grants before throttling kicks
                # in, matching typical token-bucket warmup semantics.
                state = _BucketState(tokens=self._cap, last=now)
                self._state[key] = state

            elapsed = now - state.last
            if elapsed > 0:
                state.tokens = min(self._cap, state.tokens + elapsed * self._rate)
                state.last = now

            if state.tokens >= 1.0:
                state.tokens -= 1.0
                return True
            return False

    def wait_for(self, provider: str, endpoint: str) -> timedelta:
        """Seconds until the next token would be available.  Informational."""
        with self._lock:
            key = self._key(provider, endpoint)
            state = self._state.get(key)
            if state is None or state.tokens >= 1.0:
                return timedelta()
            needed = 1.0 - state.tokens
            seconds = needed / self._rate
            return timedelta(seconds=seconds)
