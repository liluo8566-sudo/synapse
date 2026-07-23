from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Any

from .base import Provider
from .errors import ProviderDeadError

_MOCK_SID = "mock-sid-0001"


class EchoProvider(Provider):
    """In-memory echo provider for tests and dry-runs. No subprocess."""

    def __init__(self) -> None:
        self.alive: bool = False
        self.session_id: str | None = None
        self.usage_total: dict[str, int] = {}
        self._queue: deque[dict[str, Any]] = deque()

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.alive = True

    def send(self, msg: str) -> None:
        if not self.alive:
            raise ProviderDeadError("echo provider not spawned")
        self._queue.append(
            {"type": "system", "subtype": "init", "session_id": _MOCK_SID}
        )
        self._queue.append(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": f"echo: {msg}"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        self._queue.append(
            {"type": "result", "result": f"echo: {msg}", "session_id": _MOCK_SID}
        )

    def recv(self, first_line: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.alive:
            raise ProviderDeadError("echo provider not spawned")
        while self._queue:
            ev = self._queue.popleft()
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id")
            elif t == "assistant":
                usage = ev.get("message", {}).get("usage") or {}
                for k, v in usage.items():
                    if isinstance(v, int):
                        self.usage_total[k] = self.usage_total.get(k, 0) + v
            yield ev
            if t == "result":
                return

    def cancel(self) -> None:
        self._queue.clear()

    def close(self) -> None:
        self.alive = False
        self._queue.clear()

    def is_alive(self) -> bool:
        return bool(self.alive)
