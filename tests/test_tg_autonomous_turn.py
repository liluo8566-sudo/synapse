"""Tests for TgLoop.check_autonomous_turn + _deliver_reply."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from synapse_core.providers.errors import ProviderDeadError
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> Any:
        self.messages.append(kwargs)
        return type("SentMsg", (), {"message_id": len(self.messages)})()

    async def send_chat_action(self, **_kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


class AutonomousProvider:
    """Provider with one pre-buffered autonomous turn."""

    def __init__(self, reply: str) -> None:
        self.alive = True
        self.session_id: str | None = None
        self.usage_total: dict[str, int] = {}
        self._reply = reply
        self._pending = True
        self.recv_called = 0

    def spawn(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def has_complete_turn(self) -> bool:
        return self._pending

    def send(self, msg: str) -> None:
        pass

    def recv(self) -> Iterator[dict[str, Any]]:
        self.recv_called += 1
        if not self._pending:
            yield {"type": "result", "result": ""}
            return
        self._pending = False
        yield {"type": "system", "subtype": "init", "session_id": "auto-sid"}
        yield {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": self._reply}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        yield {"type": "result", "result": self._reply}

    def cancel(self) -> None:
        self.alive = False

    def close(self) -> None:
        self.alive = False


def _make_loop(tmp_path: Path) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    return loop


def test_autonomous_turn_delivered_when_pending_chat_id_set(tmp_path: Path) -> None:
    """Buffered autonomous turn + lock free + _pending_chat_id set → bot receives message."""
    loop = _make_loop(tmp_path)
    provider = AutonomousProvider("wake up!")
    loop._provider = provider  # type: ignore[assignment]
    loop._pending_chat_id = 42
    bot = FakeBot()
    loop._bot = bot  # type: ignore[assignment]

    assert provider.has_complete_turn()
    asyncio.run(loop.check_autonomous_turn(FakeContext(bot)))  # type: ignore[arg-type]

    assert bot.messages, "bot should have received at least one message"
    texts = [m["text"] for m in bot.messages if "text" in m]
    assert any("wake up!" in t for t in texts)
    assert not provider.has_complete_turn(), "turn must be consumed"


def test_autonomous_turn_skipped_when_lock_held(tmp_path: Path) -> None:
    """Lock held → check_autonomous_turn returns immediately without draining."""
    loop = _make_loop(tmp_path)
    provider = AutonomousProvider("should not arrive")
    loop._provider = provider  # type: ignore[assignment]
    loop._pending_chat_id = 42
    bot = FakeBot()
    loop._bot = bot  # type: ignore[assignment]

    async def _run() -> None:
        async with loop._lock:
            await loop.check_autonomous_turn(FakeContext(bot))

    asyncio.run(_run())

    assert not bot.messages, "no messages should be sent when lock is held"
    assert provider.has_complete_turn(), "turn must NOT be drained when lock held"
    assert provider.recv_called == 0


def test_autonomous_turn_drained_but_not_sent_when_no_pending_chat_id(tmp_path: Path) -> None:
    """_pending_chat_id None → turn is drained but bot gets no sends."""
    loop = _make_loop(tmp_path)
    provider = AutonomousProvider("silent")
    loop._provider = provider  # type: ignore[assignment]
    loop._pending_chat_id = None
    bot = FakeBot()
    loop._bot = bot  # type: ignore[assignment]

    asyncio.run(loop.check_autonomous_turn(FakeContext(bot)))  # type: ignore[arg-type]

    assert not bot.messages, "bot should not receive messages when pending_chat_id is None"
    assert not provider.has_complete_turn(), "turn must still be drained"
