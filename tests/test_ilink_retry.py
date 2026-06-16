"""Retry framework tests."""

from __future__ import annotations

import time

import httpx
import pytest

from synapse_wx.ilink.retry import with_retry


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Stub time.sleep to record delays without waiting."""
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda d: delays.append(d))
    return delays


def test_first_try_success_no_sleep(_no_sleep: list[float]) -> None:
    calls = {"n": 0}

    @with_retry(attempts=5)
    def f() -> str:
        calls["n"] += 1
        return "ok"

    assert f() == "ok"
    assert calls["n"] == 1
    assert _no_sleep == []


def test_fails_twice_then_succeeds(_no_sleep: list[float]) -> None:
    calls = {"n": 0}

    @with_retry(attempts=5, base_delay=0.1, max_delay=1.0)
    def f() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert f() == "ok"
    assert calls["n"] == 3
    assert len(_no_sleep) == 2  # 2 retries -> 2 sleeps


def test_exhausts_attempts_calls_on_failure_and_reraises(
    _no_sleep: list[float],
) -> None:
    calls = {"n": 0}
    failures: list[tuple[Exception, int]] = []

    def on_fail(exc: Exception, attempts: int) -> None:
        failures.append((exc, attempts))

    @with_retry(attempts=3, base_delay=0.01, on_failure=on_fail)
    def f() -> None:
        calls["n"] += 1
        raise httpx.ConnectError(f"fail {calls['n']}")

    with pytest.raises(httpx.ConnectError, match="fail 3"):
        f()
    assert calls["n"] == 3
    assert len(failures) == 1
    assert failures[0][1] == 3
    assert isinstance(failures[0][0], httpx.ConnectError)
    assert len(_no_sleep) == 2  # attempts-1 sleeps before final failure


def test_non_retryable_raises_immediately(_no_sleep: list[float]) -> None:
    calls = {"n": 0}

    @with_retry(attempts=5, retry_on=(httpx.HTTPError,))
    def f() -> None:
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        f()
    assert calls["n"] == 1
    assert _no_sleep == []


def test_on_failure_hook_exceptions_are_swallowed(_no_sleep: list[float]) -> None:
    def bad_hook(exc: Exception, attempts: int) -> None:
        raise RuntimeError("hook blew up")

    @with_retry(attempts=2, base_delay=0.01, on_failure=bad_hook)
    def f() -> None:
        raise httpx.ConnectError("real failure")

    # Original exception must propagate, not the hook's
    with pytest.raises(httpx.ConnectError, match="real failure"):
        f()


def test_method_decoration_preserves_self() -> None:
    class Box:
        def __init__(self) -> None:
            self.tries = 0

        @with_retry(attempts=3, base_delay=0.01)
        def go(self) -> str:
            self.tries += 1
            if self.tries < 2:
                raise httpx.ConnectError("again")
            return f"done-{self.tries}"

    b = Box()
    assert b.go() == "done-2"
    assert b.tries == 2
