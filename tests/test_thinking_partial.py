"""Plaintext thinking via cc --include-partial-messages.

Under OAuth the final assistant `thinking` block is empty (signature-only).
The plaintext only arrives as live `stream_event` → `content_block_delta` →
`thinking_delta` chunks. _drain_recv must accumulate those into
self._last_thinking so maybe_flush can emit the 思考 bubble.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.base import Provider
from synapse_core.providers.cc import ClaudeCodeProvider
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class _ThinkingDeltaProvider(Provider):
    """Mock that emits init → thinking_delta chunks → empty thinking block → result."""

    def __init__(self, deltas: list[str]) -> None:
        self.alive = False
        self._deltas = deltas
        self._queue: deque[dict[str, Any]] = deque()
        self.usage_total: dict[str, int] = {}
        self.session_id: str | None = None

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.alive = True

    def send(self, msg: str) -> None:
        if not self.alive:
            raise ProviderDeadError("not spawned")
        self._queue.append({
            "type": "system", "subtype": "init",
            "session_id": "sid-thinking-0001",
            "model": "claude-opus-4-7[1m]",
        })
        # content_block_start for thinking
        self._queue.append({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        })
        # plaintext thinking_delta chunks
        for d in self._deltas:
            self._queue.append({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": d},
                },
            })
        # final empty (redacted) assistant thinking block — OAuth shape
        self._queue.append({
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-7[1m]",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig=="},
                    {"type": "text", "text": "回复正文"},
                ],
            },
        })
        self._queue.append({"type": "result", "session_id": "sid-thinking-0001"})

    def recv(self) -> Iterator[dict[str, Any]]:
        if not self.alive:
            raise ProviderDeadError("not spawned")
        while self._queue:
            ev = self._queue.popleft()
            yield ev
            if ev.get("type") == "result":
                return

    def cancel(self) -> None:
        self._queue.clear()

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class _SilentILink:
    def poll_messages(self) -> list[dict]:
        return []

    def send_text(self, *_a, **_k) -> bool:
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


@pytest.fixture()
def loop_env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    clock = lambda: 1000.0  # noqa: E731
    deltas = ["让我想想——", "这件事的核心是…", "好的，定了。"]
    factory = lambda **_kw: _ThinkingDeltaProvider(list(deltas))  # noqa: E731
    loop = MainLoop(
        ilink=_SilentILink(),
        provider_factory=factory,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    loop._provider = factory()
    loop._provider.spawn()
    return loop, state, "".join(deltas)


def test_thinking_delta_accumulates_into_last_thinking(loop_env) -> None:
    loop, _state, expected = loop_env
    loop._provider.send("ping")
    reply = loop._drain_recv()
    # plaintext thinking is reassembled from stream_event deltas
    assert loop._last_thinking == expected
    # text reply still surfaces from the final assistant frame
    assert reply == "回复正文"


def test_empty_thinking_delta_ignored(loop_env, tmp_path: Path) -> None:
    """A delta with empty/missing thinking string must not append."""
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    clock = lambda: 1000.0  # noqa: E731
    factory = lambda **_kw: _ThinkingDeltaProvider(["", "real chunk", ""])  # noqa: E731
    from synapse_core.debounce import InboundBuffer as _IB
    loop = MainLoop(
        ilink=_SilentILink(), provider_factory=factory, state=state,
        sessions=sessions, idle_loop=None, buffer=_IB(clock=clock),
        poll_interval_sec=0.01, clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None, alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    loop._provider = factory()
    loop._provider.spawn()
    loop._provider.send("ping")
    loop._drain_recv()
    assert loop._last_thinking == "real chunk"


def test_cc_build_cmd_includes_partial_messages_flag() -> None:
    """Sanity: cc spawn line carries --include-partial-messages."""
    p = ClaudeCodeProvider(
        model="claude-opus-4-6[1m]",
        effort_level="high",
        cwd="/tmp",
        channel="test",
        stderr_log=None,
    )
    cmd = p._build_cmd()
    assert "--include-partial-messages" in cmd
    assert "--effort" in cmd
    assert "high" in cmd
