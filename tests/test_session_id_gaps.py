"""Tests for two session-id persistence gaps.

Hole 1 (warmup): shell_respawn schedules a _warmup_session task that fires
feed_turn with a silent body, forcing system{init} through the init-handling
path so session_id is persisted before any bridge restart.

Hole 2 (guard fallback): the sessions.json write guard inside _handle_init_event
falls back to state.chat_id when _pending_chat_id is None, and logs a warning
instead of silently skipping when both are absent.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from synapse_core.sessionend.tracker import SessionTracker
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class _StubTyping:
    running = True

    def __init__(self, bot, chat_id):
        pass

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def stub_typing(monkeypatch):
    monkeypatch.setattr("synapse_tg.loop.TypingAction", _StubTyping)


class _NoSpawnProvider:
    """Stub that never spawns a real process."""

    alive = True
    session_id = None
    turn_output_capped = False

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.extra_env = kwargs.get("extra_env") or {}

    def is_alive(self):
        return True

    def spawn(self):
        pass

    def cancel(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch):
    monkeypatch.setattr("synapse_tg.loop.ClaudeCodeProvider", _NoSpawnProvider)


def _cfg(tmp_path, **kw):
    base = dict(data_dir=tmp_path / "tg-data")
    base.update(kw)
    return TgConfig(**base)


# ── Hole 2: _session_write_chat_id fallback ──────────────────────────────────


def test_handle_init_writes_sessions_using_state_chat_id_fallback(tmp_path):
    """_handle_init_event must fall back to state.chat_id when _pending_chat_id
    is None so a bridge restart during the respawn gap can still record the
    new session in sessions.json."""
    sessions_path = tmp_path / "sessions.json"
    tracker = SessionTracker(state_path=sessions_path)
    cfg = _cfg(tmp_path)
    loop = TgLoop(cfg, sessions=tracker)

    # Simulate a state where _pending_chat_id was reset (e.g. after respawn),
    # but state.chat_id is still persisted from before the respawn.
    loop._pending_chat_id = None
    loop._state.chat_id = 7777

    loop._handle_init_event({
        "type": "system", "subtype": "init", "session_id": "new-sid-after-respawn",
    })

    assert tracker.get("7777") == "new-sid-after-respawn", (
        "sessions.json must be written using the state.chat_id fallback"
    )


def test_handle_init_logs_warning_and_skips_when_no_chat_id_available(
    tmp_path, caplog
):
    """When both _pending_chat_id and state.chat_id are None (cold-start /
    first-ever boot), sessions.json write is skipped with a logged warning
    instead of silently doing nothing."""
    sessions_path = tmp_path / "sessions.json"
    tracker = SessionTracker(state_path=sessions_path)
    cfg = _cfg(tmp_path)          # no chat_id
    loop = TgLoop(cfg, sessions=tracker)

    loop._pending_chat_id = None
    assert loop._state.chat_id is None

    with caplog.at_level(logging.WARNING, logger="synapse_tg.loop"):
        loop._handle_init_event({
            "type": "system", "subtype": "init", "session_id": "orphan-sid",
        })

    assert "no chat_id available" in caplog.text, (
        "must log a warning when sessions.json write is skipped"
    )
    assert tracker.snapshot() == {}, "sessions.json must not be written"


def test_drain_recv_init_also_uses_state_chat_id_fallback(tmp_path):
    """_drain_recv has its own copy of the sessions.json write path; it must
    also use the _session_write_chat_id helper so the fallback applies there."""
    sessions_path = tmp_path / "sessions.json"
    tracker = SessionTracker(state_path=sessions_path)
    cfg = _cfg(tmp_path)
    loop = TgLoop(cfg, sessions=tracker)

    loop._pending_chat_id = None
    loop._state.chat_id = 8888

    # Drive _drain_recv directly via a scripted provider.
    class _ScriptedProvider:
        alive = True
        session_id = None
        turn_output_capped = False

        def recv(self, first_line=None):
            yield {"type": "system", "subtype": "init", "session_id": "drain-sid"}
            yield {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "hi"}], "usage": {}}}
            yield {"type": "result", "result": "hi"}

        def is_alive(self):
            return True

    loop._provider = _ScriptedProvider()
    text, _thinking = loop._drain_recv()

    assert tracker.get("8888") == "drain-sid", (
        "_drain_recv sessions.json write must use state.chat_id fallback"
    )


# ── Hole 1: shell_respawn schedules warmup ──────────────────────────────────


def test_shell_respawn_schedules_warmup_task(tmp_path):
    """shell_respawn must schedule _warmup_session as an asyncio task so the
    new session's system{init} is persisted before any bridge restart."""
    cfg = _cfg(tmp_path, chat_id=5555)
    loop = TgLoop(cfg)

    warmup_called: list[str] = []

    async def _fake_feed_turn(body: str) -> bool:
        warmup_called.append(body)
        return True

    async def run():
        loop.feed_turn = _fake_feed_turn

        class _P:
            alive = True
            session_id = None

            def spawn(self):
                pass

            def cancel(self):
                pass

        loop._provider = _P()
        loop._make_provider = lambda: _P()
        loop.shell_respawn()
        # Yield control so the scheduled task can run.
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(warmup_called) == 1, "warmup feed_turn must be called exactly once"
    assert "warmup" in warmup_called[0] or "silent" in warmup_called[0], (
        "warmup body must hint at silence"
    )


def test_shell_respawn_warmup_persists_session_id(tmp_path):
    """After shell_respawn, _warmup_session must cause session_id to be written
    to bridge_state.json (simulating the init event arriving)."""
    sessions_path = tmp_path / "sessions.json"
    tracker = SessionTracker(state_path=sessions_path)
    cfg = _cfg(tmp_path, chat_id=5556)
    loop = TgLoop(cfg, sessions=tracker)
    loop._pending_chat_id = 5556
    loop._state.chat_id = 5556

    # Stub feed_turn to simulate a warmup that adopts the new session_id.
    async def _fake_feed_turn(body: str) -> bool:
        # Simulate what _handle_init_event would do when init arrives.
        loop._state.session_id = "warmed-sid"
        loop._persist_state()
        if loop._sessions is not None:
            cid = loop._session_write_chat_id()
            if cid is not None:
                loop._sessions.set(str(cid), "warmed-sid")
        return True

    async def run():
        loop.feed_turn = _fake_feed_turn

        class _P:
            alive = True
            session_id = None

            def spawn(self):
                pass

            def cancel(self):
                pass

        loop._provider = _P()
        loop._make_provider = lambda: _P()
        loop.shell_respawn()
        await asyncio.sleep(0)

    asyncio.run(run())

    assert loop._state.session_id == "warmed-sid"
    assert tracker.get("5556") == "warmed-sid", (
        "sessions.json must record the new session after warmup"
    )


def test_shell_respawn_warmup_is_noop_without_event_loop(tmp_path):
    """When shell_respawn is called outside any running event loop (e.g. in a
    sync test), the warmup scheduling must be silently skipped — no crash."""
    cfg = _cfg(tmp_path)
    loop = TgLoop(cfg)

    class _P:
        alive = True
        session_id = None

        def spawn(self):
            pass

        def cancel(self):
            pass

    loop._provider = _P()
    loop._make_provider = lambda: _P()

    # Must not raise even without a running event loop.
    loop.shell_respawn()
