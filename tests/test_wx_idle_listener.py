"""T5 (wx port of T3): resident idle listener drains unsolicited turns between
sends.

Mock at the provider boundary (poll_line + recv yield dicts); never spawn
claude, never touch real WeChat. Provider death surfaces as POLL_EOF; the
listener never dies from an exception and re-reads self._provider every call.
TypingPing is stubbed so no real re-ping thread / real sleep runs.
"""

from __future__ import annotations

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_core.providers.cc import POLL_EOF
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.config import Config
from synapse_wx.loop import MainLoop


class FakeILink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.typing = 0

    def send_text(self, to_user_id, ctx_token, text, **_kwargs) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    def send_typing(self, *_a, **_k) -> None:
        self.typing += 1


class _StubTyping:
    """No-op typing indicator; fires one ping on start so tests can assert
    typing ran. No background thread / real sleep (mirrors the tg lesson)."""

    def __init__(self, ilink, to_user_id, context_token, interval=5.0) -> None:
        self._ilink = ilink
        self.started = False

    def start(self) -> None:
        self.started = True
        try:
            self._ilink.send_typing(None, None)
        except Exception:
            pass

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def stub_typing(monkeypatch):
    monkeypatch.setattr("synapse_wx.loop.TypingPing", _StubTyping)


@pytest.fixture(autouse=True)
def one_bubble_split(monkeypatch):
    monkeypatch.setattr(
        "synapse_wx.loop.split_for_wechat_typed",
        lambda text: [{"kind": "text", "text": text}],
    )


def _turn_events(text, *, unsolicited=True, sid="sid-x"):
    """Return a list of already-parsed event dicts, mirroring what poll_line
    returns (the reader thread pre-parses JSON into dicts)."""
    events = []
    if unsolicited:
        events.append({"type": "system", "subtype": "task_notification"})
    events.append({"type": "system", "subtype": "init", "session_id": sid})
    events.append({"type": "assistant",
                   "message": {"content": [{"type": "text", "text": text}]}})
    events.append({"type": "result", "result": text})
    return events


# Keep old name as alias so individual tests don't need rewriting.
_turn_lines = _turn_events


class QueueProvider:
    """poll_line pops one event dict; recv(first_line) consumes first_line dict
    then the queue until a result. Mirrors the real ClaudeCodeProvider contract:
    the reader thread pre-parses JSON, so poll_line returns dicts, never strings."""

    def __init__(self, events: list) -> None:
        self._lines = list(events)
        self.alive = True
        self.session_id = None
        self.turn_output_capped = False
        self.usage_total: dict = {}

    def poll_line(self, timeout):
        if not self._lines:
            return None
        item = self._lines.pop(0)
        if item is POLL_EOF:
            self.alive = False
            return POLL_EOF
        return item  # already a dict

    def recv(self, first_line=None):
        if first_line is not None:
            ev0 = first_line  # already a dict (poll_line returns dicts)
            yield ev0
            if ev0.get("type") == "result":
                return
        while self._lines:
            item = self._lines.pop(0)
            if item is POLL_EOF:
                return
            ev = item  # already a dict
            yield ev
            if ev.get("type") == "result":
                return

    def send(self, msg):
        return None

    def is_alive(self):
        return self.alive


def _loop(tmp_path, alerts=None) -> MainLoop:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = FakeILink()
    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda *_a, **_k: None,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        alerts=alerts,
        cfg=Config(),
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        media_dir=tmp_path / "media",
    )
    loop._last_from_wxid = "lumi"
    loop._last_ctx_token = "ctx-1"
    return loop


def test_idle_unsolicited_delivered_without_inbound(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = QueueProvider(_turn_lines("background answer"))
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["background answer"]
    # typing indicator ran during generation
    assert loop._ilink.typing >= 1


class NoRecvProvider(QueueProvider):
    """Provider whose recv must never be entered (blocking drain forbidden)."""

    def recv(self, first_line=None):
        raise AssertionError("recv entered for a non-turn first line")


def test_init_handshake_does_not_start_typing_or_drain(tmp_path):
    """Every spawn (fresh or --resume) emits system{init} first. The listener
    must consume it without typing and without entering recv (which would block
    idle_hard_s then SIGKILL the fresh process)."""
    loop = _loop(tmp_path)
    prov = NoRecvProvider(
        [{"type": "system", "subtype": "init",
          "session_id": "sid-new", "model": "opus"}]
    )
    loop._provider = prov
    loop._listen_once()
    assert loop._ilink.typing == 0
    assert loop._ilink.sent == []
    assert loop._listen_typing is None
    # State still mirrored from the handshake; provider stays healthy.
    assert loop.state.session_id == "sid-new"
    assert prov.model_actual == "opus"
    assert prov.alive is True
    # Next iteration keeps working (loop stayed healthy).
    loop._listen_once()
    assert loop._ilink.sent == []


def test_non_turn_first_event_consumed_not_drained(tmp_path):
    """A stray non-task_notification event dict (e.g. stream_event, a non-dict)
    is consumed without typing or draining.

    Note: raw strings can no longer arrive via poll_line — the reader thread
    pre-parses JSON and skips bad lines before enqueueing. Only dicts reach here.
    The non-dict guard in _consume_non_turn_line is a defensive belt-and-braces
    check for future misuse, tested with a synthetic non-dict sentinel."""
    loop = _loop(tmp_path)
    # A non-dict value (defensive check) and a stray stream_event dict.
    prov = NoRecvProvider([42,  # non-dict: should be consumed with a warning
                           {"type": "stream_event"}])
    loop._provider = prov
    loop._listen_once()
    loop._listen_once()
    assert loop._ilink.typing == 0
    assert loop._ilink.sent == []


def test_handshake_before_unsolicited_turn_still_delivers(tmp_path):
    """A handshake queued ahead of a real unsolicited turn must not eat it."""
    loop = _loop(tmp_path)
    lines = [{"type": "system", "subtype": "init",
              "session_id": "sid-new"}] + _turn_lines("bg answer")
    loop._provider = QueueProvider(lines)
    loop._listen_once()  # consumes the handshake only
    assert loop._ilink.sent == []
    loop._listen_once()  # now the real turn
    assert [s[2] for s in loop._ilink.sent] == ["bg answer"]
    assert loop._ilink.typing >= 1


def test_idle_none_poll_is_noop(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = QueueProvider([])  # poll_line -> None
    loop._listen_once()
    assert loop._ilink.sent == []


def test_poll_eof_marks_dead_no_respawn(tmp_path):
    loop = _loop(tmp_path)
    prov = QueueProvider([POLL_EOF])
    loop._provider = prov
    loop._listen_once()
    assert prov.alive is False
    # Listener does NOT respawn; provider object stays (lazy respawn on send).
    assert loop._provider is prov
    assert loop._ilink.sent == []


def test_consecutive_back_to_back_turns(tmp_path):
    loop = _loop(tmp_path)
    lines = _turn_lines("bg one") + _turn_lines("bg two")
    loop._provider = QueueProvider(lines)
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["bg one", "bg two"]


def test_no_chat_target_drops_with_warning_no_crash(tmp_path):
    loop = _loop(tmp_path)
    loop._last_from_wxid = None
    loop._provider = QueueProvider(_turn_lines("orphan"))
    # Must not raise; turn drained, nothing sent (no chat target).
    loop._listen_once()
    assert loop._ilink.sent == []


def test_dead_provider_is_noop(tmp_path):
    loop = _loop(tmp_path)
    prov = QueueProvider(_turn_lines("x"))
    prov.alive = False
    loop._provider = prov
    loop._listen_once()
    assert loop._ilink.sent == []


def test_none_provider_is_noop(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = None
    loop._listen_once()  # no crash
    assert loop._ilink.sent == []


def test_exception_in_delivery_does_not_kill_listener(tmp_path):
    """A delivery blow-up in one iteration is caught by _idle_listener's
    catch-all; the loop keeps running and exits cleanly on the stop event."""
    loop = _loop(tmp_path)
    loop._provider = QueueProvider(_turn_lines("boom"))

    def bad_deliver(*a, **k):
        raise RuntimeError("delivery blew up")

    loop._deliver_reply = bad_deliver  # type: ignore[assignment]

    # Drive one guarded iteration then stop: _idle_listener must return without
    # the exception escaping.
    calls = {"n": 0}
    orig_listen = loop._listen_once

    def guarded_listen():
        calls["n"] += 1
        try:
            orig_listen()
        finally:
            loop._stop_evt.set()

    loop._listen_once = guarded_listen  # type: ignore[assignment]
    loop._idle_listener()
    assert calls["n"] == 1


def test_provider_swapped_mid_poll_picked_up_next_iteration(tmp_path):
    """The listener must re-read self._provider inside the lock. Swap the
    provider before the iteration; the new object is used, no crash."""
    loop = _loop(tmp_path)
    loop._provider = QueueProvider([])
    # Simulate a swap: replace with a fresh provider that has a real turn.
    loop._provider = QueueProvider(_turn_lines("after swap"))
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["after swap"]


# ── Regression: poll_line returns dicts, not strings ──────────────────────────

def test_poll_line_returns_dict_not_str_regression(tmp_path):
    """Regression: poll_line returns already-parsed dicts (the reader thread
    pre-parses JSON before enqueueing). _consume_non_turn_line must accept a
    dict directly and MUST NOT call .strip() or json.loads() on it.

    Before the fix this raised:
        AttributeError: 'dict' object has no attribute 'strip'
    repeating on every idle listener iteration.
    """
    loop = _loop(tmp_path)
    # Feed a raw dict init handshake — exactly what ClaudeCodeProvider.poll_line
    # returns after the upstream reader-thread change.
    init_dict = {"type": "system", "subtype": "init", "session_id": "sid-dict"}
    prov = NoRecvProvider([init_dict])
    loop._provider = prov

    # Must not raise; must consume the handshake cleanly.
    loop._listen_once()

    assert loop._ilink.typing == 0
    assert loop._ilink.sent == []
    assert loop.state.session_id == "sid-dict"


def test_poll_line_dict_unsolicited_turn_delivered(tmp_path):
    """End-to-end: an unsolicited turn delivered as pre-parsed dicts (matching
    the real poll_line contract) is collected and sent without error."""
    loop = _loop(tmp_path)
    # _turn_events already returns dicts; verify the full path end-to-end.
    loop._provider = QueueProvider(_turn_events("hello from dict turn"))
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["hello from dict turn"]
    assert loop._ilink.typing >= 1
