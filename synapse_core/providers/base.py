from __future__ import annotations

import abc
from collections.abc import Iterator
from typing import Any


class Provider(abc.ABC):
    """Persistent LLM subprocess wrapper. One instance = one running CLI."""

    @abc.abstractmethod
    def spawn(self, env: dict[str, str] | None = None) -> None:
        """Start the underlying subprocess. Raises on spawn failure."""

    @abc.abstractmethod
    def send(self, msg: str) -> None:
        """Write a user message into the subprocess stdin."""

    @abc.abstractmethod
    def recv(self, first_line: str | None = None) -> Iterator[dict[str, Any]]:
        """Yield events from stdout until the turn's result event.

        Raises if subprocess dies mid-stream.
        """

    @abc.abstractmethod
    def cancel(self) -> None:
        """Best-effort interrupt of the current turn. Phase A = kill+respawn."""

    @abc.abstractmethod
    def close(self) -> None:
        """Graceful shutdown: stdin.end -> SIGTERM -> SIGKILL."""

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """Return True if the underlying subprocess is still running."""
