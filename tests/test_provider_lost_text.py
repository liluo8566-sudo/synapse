"""Lost-text detection: `ClaudeCodeProvider.turn_lost_text` and `is_turn_event`.

Reuses the fake-subprocess approach from test_provider_turn_cap.py: a scripted
stdout pipe yields stream-json lines so the provider's real recv() loop can be
exercised without a real cc. Idle thresholds are set generous so only the
lost-text logic is under test.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from synapse_core.providers.cc import ClaudeCodeProvider, is_turn_event


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _assistant_block(
    block: dict, *, msg_id: str = "msg-1", parent_tool_use_id: str | None = None
) -> str:
    return _line(
        {
            "type": "assistant",
            "parent_tool_use_id": parent_tool_use_id,
            "message": {
                "id": msg_id,
                "content": [block],
                "usage": {"output_tokens": 1},
            },
        }
    )


def _result(text: str = "ok") -> str:
    return _line({"type": "result", "result": text})


def _provider(lines: list[str]):
    """Spawn a provider whose stdout replays `lines` immediately (no delays)."""
    p = ClaudeCodeProvider(
        channel="test",
        cwd="/tmp",
        stderr_log=None,
        idle_soft_s=30.0,
        idle_hard_s=60.0,
    )
    fake = MagicMock()
    fake.stdin = MagicMock()
    fake.stdin.closed = False
    fake.stdout = iter(lines)
    fake.stderr = MagicMock()
    fake.pid = 12345
    fake.poll.return_value = None
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = fake
        p.spawn()
    return p, fake


_THINKING = {"type": "thinking"}
_TEXT = {"type": "text", "text": "hi"}
_TOOL_USE = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}


def test_double_thinking_same_message_sets_lost_text():
    """Two consecutive thinking blocks (one per event) for the same message id
    is the lost-text pattern — the user-facing reply likely landed in the
    second thinking block instead of a text block."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant_block(_THINKING),
        _assistant_block(_THINKING),
        _assistant_block(_TOOL_USE),
        _result(),
    ]
    p, _fake = _provider(lines)
    list(p.recv())
    assert p.turn_lost_text is True


def test_single_thinking_then_tool_use_is_not_lost_text():
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant_block(_THINKING),
        _assistant_block(_TOOL_USE),
        _result(),
    ]
    p, _fake = _provider(lines)
    list(p.recv())
    assert p.turn_lost_text is False


def test_thinking_text_thinking_tool_use_is_not_lost_text():
    """A text block in between the two thinking blocks means the reply did
    reach a text block — not the lost-text pattern."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant_block(_THINKING),
        _assistant_block(_TEXT),
        _assistant_block(_THINKING),
        _assistant_block(_TOOL_USE),
        _result(),
    ]
    p, _fake = _provider(lines)
    list(p.recv())
    assert p.turn_lost_text is False


def test_double_thinking_different_message_ids_is_not_lost_text():
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant_block(_THINKING, msg_id="msg-1"),
        _assistant_block(_THINKING, msg_id="msg-2"),
        _result(),
    ]
    p, _fake = _provider(lines)
    list(p.recv())
    assert p.turn_lost_text is False


def test_subagent_double_thinking_is_not_lost_text():
    """Subagent events (parent_tool_use_id set) are excluded — the same double
    thinking pattern from a dispatched subagent must not false-trigger."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant_block(_THINKING, parent_tool_use_id="toolu_sub"),
        _assistant_block(_THINKING, parent_tool_use_id="toolu_sub"),
        _result(),
    ]
    p, _fake = _provider(lines)
    list(p.recv())
    assert p.turn_lost_text is False


def test_lost_text_resets_on_next_recv():
    """A turn that set turn_lost_text must not leak it into the next turn."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant_block(_THINKING),
        _assistant_block(_THINKING),
        _assistant_block(_TOOL_USE),
        _result(),
    ]
    p, fake = _provider(lines)
    list(p.recv())
    assert p.turn_lost_text is True

    # Feed a fresh, clean turn as if the resident reader had queued it.
    import queue as _queue

    try:
        while True:
            p._event_queue.get_nowait()
    except _queue.Empty:
        pass
    for ln in [_assistant_block(_THINKING), _assistant_block(_TOOL_USE), _result()]:
        ev = json.loads(ln)
        if ev.get("type") == "result":
            with p._turn_lock:
                p._complete_turns += 1
        p._event_queue.put(ev)
    list(p.recv())
    assert p.turn_lost_text is False


# ── is_turn_event ────────────────────────────────────────────────────────────

def test_is_turn_event_true_for_main_thread_turn_events():
    for t in ("stream_event", "assistant", "user", "result"):
        assert is_turn_event({"type": t, "parent_tool_use_id": None}) is True
        # Real events always carry the key, but a missing key (absent in
        # synthetic/old events) still means "not a subagent".
        assert is_turn_event({"type": t}) is True


def test_is_turn_event_false_for_out_of_band_system_frames():
    for subtype in (
        "init",
        "status",
        "background_tasks_changed",
        "task_updated",
        "task_progress",
        "task_started",
    ):
        assert is_turn_event({"type": "system", "subtype": subtype}) is False


def test_is_turn_event_false_for_rate_limit_event():
    assert is_turn_event({"type": "rate_limit_event"}) is False


def test_is_turn_event_false_for_subagent_events():
    assert (
        is_turn_event({"type": "stream_event", "parent_tool_use_id": "toolu_x"})
        is False
    )
    assert (
        is_turn_event({"type": "assistant", "parent_tool_use_id": "toolu_x"})
        is False
    )
