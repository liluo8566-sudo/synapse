"""WeChat 正在输入中 indicator: re-pings ilink while cc thinks."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class TypingPing:
    """Background re-pinger for the iLink typing indicator.

    Fires the first ping from inside the daemon thread (NOT inline in
    ``start()``) so the caller — typically ``MainLoop.maybe_flush`` right
    before ``provider.send`` — never blocks on iLink network latency. The
    thread re-pings every ``interval`` seconds until ``stop()``. All ilink
    calls are swallowed.
    """

    def __init__(
        self,
        ilink: Any,
        to_user_id: str,
        context_token: str,
        interval: float = 5.0,
    ) -> None:
        self._ilink = ilink
        self._to = to_user_id
        self._ctx = context_token or ""
        self._interval = interval
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._to:
            return
        logger.info("TYPING_PROBE: start at %.3f", time.monotonic())
        self._thread = threading.Thread(
            target=self._run, name="typing-ping", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()

    def _run(self) -> None:
        # First ping immediately (no wait), then re-ping on interval.
        self._ping()
        while not self._stop_evt.wait(self._interval):
            self._ping()

    def _ping(self) -> None:
        try:
            self._ilink.send_typing(self._to, self._ctx)
        except Exception:
            pass
