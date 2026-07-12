"""Bridge restart target amnesia: after a restart, `_pending_chat_id` (tg) /
`_last_from_wxid` (wx) must be restored from persisted BridgeState so periodic
jobs (qidu signal poll, heartbeat) don't sit stuck waiting for the user's next
inbound message before they can act.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from synapse_core import bridge_state_store
from synapse_core.debounce import InboundBuffer
from synapse_core.state import BridgeState
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.providers.mock import EchoProvider
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_wx.loop import MainLoop


# ── tg: chat_id persist / restore ───────────────────────────────────────────


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


class _FakePoller:
    """Mock QiduSignalPoller — records fetch() calls."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.fetch_called = False

    def should_poll(self) -> bool:
        return True

    def fetch(self) -> list[str]:
        self.fetch_called = True
        return self._texts


def test_track_persists_chat_id(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    bot = FakeBot()

    loop._track(bot, chat_id=555)  # type: ignore[arg-type]

    saved = bridge_state_store.load(loop._state_path)
    assert saved["chat_id"] == 555


def test_track_does_not_rewrite_state_when_chat_id_unchanged(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    bot = FakeBot()
    loop._track(bot, chat_id=555)  # type: ignore[arg-type]
    mtime1 = loop._state_path.stat().st_mtime_ns

    loop._track(bot, chat_id=555)  # type: ignore[arg-type]
    mtime2 = loop._state_path.stat().st_mtime_ns

    assert mtime1 == mtime2  # no disk churn on repeat same-chat messages


def test_new_loop_restores_pending_chat_id_from_persisted_state(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    first = TgLoop(cfg)
    bot = FakeBot()
    first._track(bot, chat_id=777)  # type: ignore[arg-type]

    # Simulate a bridge restart: fresh TgLoop reading the same data_dir.
    restarted = TgLoop(cfg)
    assert restarted._pending_chat_id == 777


def test_check_qidu_signal_fetches_after_restart_with_no_inbound_message(tmp_path: Path) -> None:
    """Restart scenario from the live bug: chat_id was persisted by a prior
    session, no TG message has arrived yet, but check_qidu_signal must still
    fetch instead of silently no-op'ing forever."""
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    first = TgLoop(cfg)
    first._track(FakeBot(), chat_id=999)  # type: ignore[arg-type]

    restarted = TgLoop(cfg)
    assert restarted._pending_chat_id == 999  # restored, no _track call yet

    poller = _FakePoller(["qidu reply text"])
    restarted._qidu_signal = poller  # type: ignore[assignment]
    bot = FakeBot()

    asyncio.run(restarted.check_qidu_signal(FakeContext(bot)))  # type: ignore[arg-type]

    assert poller.fetch_called
    assert restarted._buffer.flush() == "qidu reply text"


def test_check_qidu_signal_restores_bot_from_context_when_none(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    loop._pending_chat_id = 111
    assert loop._bot is None
    bot = FakeBot()

    asyncio.run(loop.check_qidu_signal(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._bot is bot


def test_check_heartbeat_restores_bot_from_context_when_none(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    assert loop._bot is None
    bot = FakeBot()

    asyncio.run(loop.check_heartbeat(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._bot is bot


# ── wx: last_from_wxid persist / restore ────────────────────────────────────


class _ILink:
    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches)

    def poll_messages(self) -> list[dict]:
        return self._batches.pop(0) if self._batches else []

    def send_text(self, to: str, ctx: str, text: str) -> bool:
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


def _build_wx_loop(state: BridgeState, tmp_path: Path, persist_state=None) -> MainLoop:
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = _ILink([[{"from_wxid": "lumi", "context_token": "ctx", "text": "hi"}]])
    return MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        persist_state=persist_state,
    )


def test_wx_tick_persists_last_from_wxid(tmp_path: Path) -> None:
    calls: list[int] = []
    state = BridgeState()
    loop = _build_wx_loop(state, tmp_path, persist_state=lambda: calls.append(1))

    loop.tick()

    assert state.last_from_wxid == "lumi"
    assert loop._last_from_wxid == "lumi"
    assert len(calls) == 1


def test_wx_restart_restores_last_from_wxid_from_persisted_state(tmp_path: Path) -> None:
    state = BridgeState(last_from_wxid="lumi")
    loop = _build_wx_loop(state, tmp_path)

    assert loop._last_from_wxid == "lumi"
