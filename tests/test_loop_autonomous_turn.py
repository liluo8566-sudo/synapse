"""Test: autonomous turns (no inbound send) are delivered via _maybe_drain_autonomous."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from synapse_core.providers.errors import ProviderDeadError
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.loop import MainLoop


class _AutonomousProvider:
    """Provider that has one pre-buffered autonomous turn, then acts as echo."""

    def __init__(self, auto_reply: str) -> None:
        self.alive = True
        self.session_id: str | None = None
        self.usage_total: dict[str, int] = {}
        self._auto_reply = auto_reply
        self._auto_pending = True

    def spawn(self, env: dict | None = None) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def has_complete_turn(self) -> bool:
        return self._auto_pending

    def send(self, msg: str) -> None:
        pass

    def recv(self) -> Iterator[dict[str, Any]]:
        if not self.alive:
            raise ProviderDeadError("dead")
        if self._auto_pending:
            self._auto_pending = False
            yield {"type": "system", "subtype": "init", "session_id": "auto-sid"}
            yield {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": self._auto_reply}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
            yield {"type": "result", "result": self._auto_reply}
            return
        yield {"type": "result", "result": ""}

    def cancel(self) -> None:
        self.alive = False

    def close(self) -> None:
        self.alive = False


class _MockILink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list:
        return []

    def send_text(self, to: str, ctx: str, text: str, **_) -> bool:
        self.sent.append((to, ctx, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


def _make_loop(
    ilink: _MockILink,
    provider: _AutonomousProvider,
    tmp_path: Path,
) -> MainLoop:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda **_: provider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        poll_interval_sec=99.0,
        wallclock=lambda: datetime(2026, 7, 6, 12, 0),
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    loop._provider = provider
    return loop


def test_autonomous_turn_delivered_to_last_wxid(tmp_path: Path) -> None:
    """_maybe_drain_autonomous delivers pre-buffered turn to _last_from_wxid
    without any inbound message triggering a send()."""
    ilink = _MockILink()
    provider = _AutonomousProvider("wake up!")
    loop = _make_loop(ilink, provider, tmp_path)

    # Simulate that a prior inbound established the target wxid.
    loop._last_from_wxid = "frost-wxid"
    loop._last_ctx_token = "ctx-99"

    assert provider.has_complete_turn()
    loop._maybe_drain_autonomous()

    # Autonomous reply should have been delivered as bubbles to last wxid.
    assert ilink.sent, "no bubbles sent for autonomous turn"
    assert ilink.sent[0][0] == "frost-wxid"
    assert "wake up!" in ilink.sent[0][2]

    # Turn consumed — counter should be zero.
    assert not provider.has_complete_turn()


def test_autonomous_turn_drained_but_not_delivered_when_no_wxid(tmp_path: Path) -> None:
    """When _last_from_wxid is None, the turn is still drained (pipe stays clean)
    but no send_text is attempted."""
    ilink = _MockILink()
    provider = _AutonomousProvider("silent wake")
    loop = _make_loop(ilink, provider, tmp_path)

    loop._last_from_wxid = None  # no recipient known yet

    loop._maybe_drain_autonomous()

    assert not ilink.sent, "should not send when wxid unknown"
    assert not provider.has_complete_turn(), "turn must still be drained"


def test_maybe_drain_noop_when_no_complete_turn(tmp_path: Path) -> None:
    """_maybe_drain_autonomous is a no-op when no turn is buffered."""
    ilink = _MockILink()
    provider = _AutonomousProvider("never sent")
    provider._auto_pending = False  # no buffered turn
    loop = _make_loop(ilink, provider, tmp_path)
    loop._last_from_wxid = "frost-wxid"

    loop._maybe_drain_autonomous()

    assert not ilink.sent
