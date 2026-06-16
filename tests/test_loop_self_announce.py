"""Restart self-announce fires from the first successful poll, not a 5s Timer.

Covers: pending+ok-poll triggers send once; subsequent polls don't re-send;
a poll that raises does not consume the pending flag.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class _RecordingILink:
    def __init__(self, msgs_per_poll: list[list[dict]] | None = None) -> None:
        self._scripted = list(msgs_per_poll or [])
        self.sends: list[tuple[str, str, str]] = []
        self.poll_calls = 0

    def poll_messages(self) -> list[dict]:
        self.poll_calls += 1
        if self._scripted:
            return self._scripted.pop(0)
        return []

    def send_text(self, to: str, ctx: str, text: str) -> bool:
        self.sends.append((to, ctx, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class _FailingPollILink(_RecordingILink):
    def __init__(self) -> None:
        super().__init__()
        self.poll_calls = 0

    def poll_messages(self) -> list[dict]:
        self.poll_calls += 1
        raise RuntimeError("ilink not ready")


@pytest.fixture()
def loop_env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    clock = lambda: 1000.0  # noqa: E731
    buf = InboundBuffer(clock=clock)
    ilink = _RecordingILink()
    loop = MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=buf,
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    return loop, ilink


def test_first_successful_poll_fires_announce(loop_env) -> None:
    loop, ilink = loop_env
    loop.arm_restart_announce("wxid_test", "我重启了")
    loop.tick()
    assert ilink.sends == [("wxid_test", "", "我重启了")]
    assert loop._announce_pending is False


def test_subsequent_polls_do_not_resend(loop_env) -> None:
    loop, ilink = loop_env
    loop.arm_restart_announce("wxid_test", "我重启了")
    loop.tick()
    loop.tick()
    loop.tick()
    assert len(ilink.sends) == 1


def test_failed_poll_does_not_consume_pending(tmp_path: Path) -> None:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    clock = lambda: 2000.0  # noqa: E731
    ilink = _FailingPollILink()
    loop = MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
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
    loop.arm_restart_announce("wxid_test", "我重启了")
    loop.tick()
    assert ilink.sends == []
    assert loop._announce_pending is True


def test_not_armed_means_no_send(loop_env) -> None:
    loop, ilink = loop_env
    loop.tick()
    assert ilink.sends == []


def test_arm_with_empty_target_is_noop(loop_env) -> None:
    loop, ilink = loop_env
    loop.arm_restart_announce("", "我重启了")
    loop.tick()
    assert ilink.sends == []
    assert loop._announce_pending is False
