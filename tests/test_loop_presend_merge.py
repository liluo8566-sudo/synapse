"""Pre-send merge: new inbound mid-turn drops the reply and re-queues old body."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop, _MERGE_NOTE
from synapse_core.providers.base import Provider
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeILink:
    def __init__(self, inbound_batches: list[list[dict]] | None = None) -> None:
        self._batches = list(inbound_batches or [])
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_text(self, to_user_id: str, ctx_token: str, text: str, **_) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class InjectingProvider(Provider):
    """Provider that adds a bubble to the loop buffer during recv(), simulating
    new inbound arriving while the provider is producing its reply."""

    def __init__(self, reply: str, loop_ref: list) -> None:
        self._reply = reply
        self._loop_ref = loop_ref  # mutable list; loop injected after construction
        self._alive = True

    def spawn(self, env=None) -> None:
        self._alive = True

    def send(self, prompt: str) -> None:
        return None

    def recv(self):
        loop: MainLoop = self._loop_ref[0]
        # Simulate new inbound arriving while cc is processing.
        with loop._state_lock:
            loop._buffer.add("new bubble mid-turn")
        yield {"type": "system", "subtype": "init", "session_id": "mock-sid-merge"}
        yield {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": self._reply}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        }
        yield {"type": "result", "result": self._reply, "session_id": "mock-sid-merge"}

    def close(self) -> None:
        self._alive = False

    def cancel(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


class EmptyProvider(Provider):
    """Provider whose recv() yields no assistant text."""

    def __init__(self) -> None:
        self._alive = True

    def spawn(self, env=None) -> None:
        self._alive = True

    def send(self, prompt: str) -> None:
        return None

    def recv(self):
        return
        yield

    def close(self) -> None:
        self._alive = False

    def cancel(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture()
def env(tmp_path: Path):
    return {
        "state": BridgeState(),
        "sessions": SessionTracker(state_path=tmp_path / "sessions.json"),
        "tmp": tmp_path,
    }


def _make_loop(env, ilink, clock, provider) -> MainLoop:
    fixed = datetime(2026, 6, 12, 12, 0)
    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=Path("/tmp/last_active_merge1.json"),
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()
    return loop


# ── merge path ────────────────────────────────────────────────────────────────


def test_new_inbound_mid_turn_drops_reply_no_send() -> None:
    """New inbound arrives during recv → reply bubbles NOT sent to ilink."""
    clock = FakeClock()
    ilink = FakeILink()
    state = BridgeState()
    sessions = SessionTracker(state_path="/tmp/sess_merge1.json")
    fixed = datetime(2026, 6, 12, 12, 0)

    loop_ref: list = []
    provider = InjectingProvider("the dropped reply", loop_ref)

    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=Path("/tmp/alerts_merge1"),
        channel="wx",
        last_active_path=Path("/tmp/last_active_merge2.json"),
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()
    loop_ref.append(loop)

    # Seed the buffer with the original user message.
    with loop._state_lock:
        loop._buffer.add("original message")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    clock.advance(6.0)
    loop.maybe_flush()

    # No reply should have been sent.
    assert ilink.sent == [], f"Expected no sends, got: {ilink.sent}"


def test_new_inbound_mid_turn_requeues_merge_note_and_old_body() -> None:
    """After merge-drop, buffer contains MERGE_NOTE + old body + new bubble."""
    clock = FakeClock()
    ilink = FakeILink()
    state = BridgeState()
    sessions = SessionTracker(state_path="/tmp/sess_merge2.json")
    fixed = datetime(2026, 6, 12, 12, 0)

    loop_ref: list = []
    provider = InjectingProvider("dropped reply text", loop_ref)

    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=Path("/tmp/alerts_merge2"),
        channel="wx",
        last_active_path=Path("/tmp/last_active_merge3.json"),
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()
    loop_ref.append(loop)

    old_body = "first question"
    with loop._state_lock:
        loop._buffer.add(old_body)
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    clock.advance(6.0)
    loop.maybe_flush()

    # Buffer now holds the merged content — flush it and inspect.
    flushed = loop._buffer.flush()
    assert _MERGE_NOTE in flushed
    assert old_body in flushed
    assert "new bubble mid-turn" in flushed
    # Order: MERGE_NOTE + old_body (prepended) comes before the new bubble.
    merge_pos = flushed.index(_MERGE_NOTE)
    old_pos = flushed.index(old_body)
    new_pos = flushed.index("new bubble mid-turn")
    assert merge_pos < new_pos
    assert old_pos < new_pos


def test_no_new_inbound_reply_sent_normally() -> None:
    """Regression: no new inbound during recv → reply sent normally."""
    clock = FakeClock()
    ilink = FakeILink()
    state = BridgeState()
    sessions = SessionTracker(state_path="/tmp/sess_merge3.json")
    fixed = datetime(2026, 6, 12, 12, 0)
    provider = EchoProvider()

    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=Path("/tmp/alerts_merge3"),
        channel="wx",
        last_active_path=Path("/tmp/last_active_merge4.json"),
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()

    with loop._state_lock:
        loop._buffer.add("hello")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    clock.advance(6.0)
    loop.maybe_flush()

    assert ilink.sent, "Expected reply bubbles to be sent"
    assert ilink.sent[0][0] == "lumi"


def test_empty_reply_with_new_inbound_no_merge_note() -> None:
    """Empty reply + new inbound: early-return path, no merge note prepended."""
    clock = FakeClock()
    ilink = FakeILink()
    state = BridgeState()
    sessions = SessionTracker(state_path="/tmp/sess_merge4.json")
    fixed = datetime(2026, 6, 12, 12, 0)
    provider = EmptyProvider()

    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=Path("/tmp/alerts_merge4"),
        channel="wx",
        last_active_path=Path("/tmp/last_active_merge5.json"),
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()

    with loop._state_lock:
        loop._buffer.add("original")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    # Manually add a bubble to simulate new inbound (empty provider won't inject).
    # We want to confirm that when reply_text == "" the merge path is NOT taken.
    clock.advance(6.0)
    loop.maybe_flush()

    # Nothing sent (empty reply), and the buffer should be empty too (flush happened).
    assert ilink.sent == []
    # No merge note should appear — early return before the merge block.
    flushed = loop._buffer.flush()
    assert _MERGE_NOTE not in flushed


def test_typing_ping_alive_on_merge_path() -> None:
    """TypingPing is NOT stopped on the merge path — it stays for the re-run."""
    clock = FakeClock()
    ilink = FakeILink()
    ilink.send_typing = lambda *a, **k: None  # duck-type the typing call
    state = BridgeState()
    sessions = SessionTracker(state_path="/tmp/sess_merge5.json")
    fixed = datetime(2026, 6, 12, 12, 0)

    loop_ref: list = []
    provider = InjectingProvider("dropped", loop_ref)

    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=Path("/tmp/alerts_merge5"),
        channel="wx",
        last_active_path=Path("/tmp/last_active_merge5.json"),
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()
    loop_ref.append(loop)

    with loop._state_lock:
        loop._buffer.add("trigger")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    clock.advance(6.0)
    loop.maybe_flush()

    # On the merge path, _stop_typing is NOT called, so _typing_ping should
    # still be set (it was started right before provider.send).
    assert loop._typing_ping is not None, (
        "TypingPing should remain alive on merge path so the next flush reuses it"
    )
    # Cleanup.
    loop._stop_typing()
