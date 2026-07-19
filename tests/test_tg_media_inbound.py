"""synapse_tg inbound media: retry helper for transient telegram network errors."""

from __future__ import annotations

import asyncio

import pytest
from telegram.error import NetworkError, TimedOut

from synapse_tg.media.inbound import _RETRY_ATTEMPTS, _with_retry


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("synapse_tg.media.inbound.asyncio.sleep", fake_sleep)
    return slept


def test_with_retry_succeeds_after_two_timeouts(no_real_sleep):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimedOut()
        return "ok"

    result = asyncio.run(_with_retry(flaky))
    assert result == "ok"
    assert calls["n"] == 3
    assert len(no_real_sleep) == 2  # backoff before attempt 2 and attempt 3


def test_with_retry_raises_after_exhausting_attempts(no_real_sleep):
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise TimedOut()

    with pytest.raises(TimedOut):
        asyncio.run(_with_retry(always_fails))
    assert calls["n"] == _RETRY_ATTEMPTS


def test_with_retry_handles_network_error_too(no_real_sleep):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise NetworkError("boom")
        return "recovered"

    result = asyncio.run(_with_retry(flaky))
    assert result == "recovered"


def test_with_retry_non_transient_error_not_retried(no_real_sleep):
    calls = {"n": 0}

    async def raises_other():
        calls["n"] += 1
        raise RuntimeError("not a network error")

    with pytest.raises(RuntimeError):
        asyncio.run(_with_retry(raises_other))
    assert calls["n"] == 1
    assert no_real_sleep == []


def test_with_retry_passes_args_and_kwargs(no_real_sleep):
    async def echo(a, b, *, c):
        return (a, b, c)

    result = asyncio.run(_with_retry(echo, 1, 2, c=3))
    assert result == (1, 2, 3)
