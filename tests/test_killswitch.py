"""Kill-switch env var tests."""

from __future__ import annotations

import pytest

from apimeter._killswitch import is_kill_switch_on


@pytest.mark.parametrize("val", ["1", "true", "True", "YES", "on", "On"])
def test_kill_switch_truthy(monkeypatch, val):
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", val)
    assert is_kill_switch_on() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "2", "random"])
def test_kill_switch_falsy(monkeypatch, val):
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", val)
    assert is_kill_switch_on() is False


def test_kill_switch_unset(monkeypatch):
    monkeypatch.delenv("DISABLE_PERMIT_SERVICE", raising=False)
    assert is_kill_switch_on() is False


def test_kill_switch_is_read_per_call(monkeypatch):
    # Important: operators flip this without restarting the pod, so the
    # value must NOT be cached at import time.
    monkeypatch.delenv("DISABLE_PERMIT_SERVICE", raising=False)
    assert is_kill_switch_on() is False
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", "true")
    assert is_kill_switch_on() is True
    monkeypatch.setenv("DISABLE_PERMIT_SERVICE", "")
    assert is_kill_switch_on() is False
