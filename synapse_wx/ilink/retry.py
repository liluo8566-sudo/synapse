"""Generic retry framework — exponential backoff + jitter, pluggable on_failure."""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RETRYABLE: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    httpx.TimeoutException,
)


def with_retry(
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[Exception], ...] = DEFAULT_RETRYABLE,
    on_failure: Callable[[Exception, int], None] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: retry callable with exponential backoff + jitter.

    Delay formula: min(base_delay * 2**attempt, max_delay) + uniform(0, 0.5).
    After all attempts fail, calls on_failure(last_exc, attempts) if given,
    then re-raises the last exception. Non-retryable exceptions raise instantly.
    Works on plain functions and instance methods.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if attempt == attempts - 1:
                        break
                    delay = min(base_delay * (2**attempt), max_delay)
                    delay += random.uniform(0, 0.5)
                    logger.warning(
                        "%s failed (attempt %d/%d): %s — retry in %.2fs",
                        func.__name__,
                        attempt + 1,
                        attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            assert last_exc is not None
            if on_failure is not None:
                try:
                    on_failure(last_exc, attempts)
                except Exception:
                    logger.exception("on_failure hook raised; ignoring")
            raise last_exc

        return wrapper

    return decorator
