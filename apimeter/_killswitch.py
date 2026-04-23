"""Operator breakglass: ``DISABLE_PERMIT_SERVICE`` env var.

Read per-call (not cached at import time) so operators can toggle it on
a running process via ``kubectl set env`` without restarting the pod.
Accepts the usual truthy spellings — case-insensitive.
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_kill_switch_on() -> bool:
    return os.getenv("DISABLE_PERMIT_SERVICE", "").strip().lower() in _TRUTHY
