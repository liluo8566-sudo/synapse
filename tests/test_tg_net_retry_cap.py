"""Cap the transient-network prepend-retry added for check_flush: at most one
retry per body, then abandon (no unbounded re-feeding of the provider during
sustained network degradation). Counter resets on the next successful turn."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram.error import NetworkError, TimedOut

from synapse_core.debounce import InboundBuffer
from synapse_core.providers.mock import EchoProvider
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return type("SentMessage", (), {"message_id": len(self.messages)})()

    async def send_chat_action(self, **_kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


class TimedOutProvider:
    """Raises TimedOut on send() every time — simulates api.telegram.org
    being unreachable for outbound calls made from inside the turn."""

    session_id = None

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.alive = True
        self.send_count = 0

    def spawn(self) -> None:
        self.alive = True

    def send(self, _body: str) -> None:
        self.send_count += 1
        raise self._exc

    def is_alive(self) -> bool:
        return self.alive

    def cancel(self) -> None:
        self.alive = False


def _loop_with_body(tmp_path: Path, clock: FakeClock, body: str = "hello") -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    loop._buffer = InboundBuffer(clock=clock)
    loop._buffer.add(body)
    clock.advance(6.0)
    loop._pending_chat_id = 123
    return loop


def test_first_network_error_prepends_body(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _loop_with_body(tmp_path, clock)
    loop._provider = TimedOutProvider(TimedOut())  # type: ignore[assignment]
    bot = FakeBot()

    asyncio.run(loop.check_flush(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._net_retry_count == 1
    assert loop._buffer.flush() == "hello"


def test_second_consecutive_network_error_does_not_prepend(tmp_path: Path) -> None:
    """After one retry is spent, a second consecutive failure abandons the
    turn instead of re-prepending — no unbounded retry loop."""
    clock = FakeClock()
    loop = _loop_with_body(tmp_path, clock)
    loop._net_retry_count = 1  # retry budget already spent
    loop._provider = TimedOutProvider(NetworkError("boom"))  # type: ignore[assignment]
    bot = FakeBot()

    asyncio.run(loop.check_flush(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._net_retry_count == 1  # unchanged — no further increment
    assert len(loop._buffer) == 0  # body NOT re-queued
    assert bot.messages == []  # nothing sent


def test_successful_turn_resets_retry_counter(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _loop_with_body(tmp_path, clock)
    loop._net_retry_count = 1
    loop._provider = EchoProvider()
    loop._provider.spawn()
    bot = FakeBot()

    asyncio.run(loop.check_flush(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._net_retry_count == 0
