"""Tests for ClaudeCodeProvider resident reader thread (Part 1 + off-by-one)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.providers.cc import ClaudeCodeProvider
from synapse_core.providers.errors import ProviderDeadError


def _make_fake_popen(stdout_lines: list[str]):
    fake = MagicMock()
    fake.stdin = MagicMock()
    fake.stdin.closed = False
    fake.stdout = iter(stdout_lines)
    fake.stderr = iter([])
    fake.poll.return_value = None
    return fake


def _provider(**kwargs) -> ClaudeCodeProvider:
    params = {"channel": "test", "cwd": "/tmp", "stderr_log": None}
    params.update(kwargs)
    return ClaudeCodeProvider(**params)


def _make_lines(*turns: list[dict]) -> list[str]:
    """Flatten turns (each a list of event dicts) into newline-terminated JSON lines."""
    lines = []
    for turn in turns:
        for ev in turn:
            lines.append(json.dumps(ev) + "\n")
    return lines


_TURN_1 = [
    {"type": "system", "subtype": "init", "session_id": "sid-t1"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 3, "output_tokens": 5}}},
    {"type": "result", "result": "hello", "session_id": "sid-t1"},
]

_TURN_2 = [
    {"type": "system", "subtype": "init", "session_id": "sid-t2"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "world"}], "usage": {"input_tokens": 4, "output_tokens": 6}}},
    {"type": "result", "result": "world", "session_id": "sid-t2"},
]


# ── Test 1: two complete turns pre-buffered ───────────────────────────────────

def test_reader_buffers_two_turns_counter_and_recv():
    """Reader thread pre-buffers both turns; has_complete_turn tracks count;
    recv() yields exactly one turn per call and decrements counter."""
    lines = _make_lines(_TURN_1, _TURN_2)

    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()

        # Drain turn 1 — blocking get() waits for reader to provide events.
        turn1 = list(p.recv())
        assert [e["type"] for e in turn1] == ["system", "assistant", "result"]
        assert turn1[-1]["result"] == "hello"

        # After recv() of turn 1, reader has processed both turns (eager reader).
        assert p.has_complete_turn(), "turn 2 should be buffered after turn 1 drained"

        # Drain turn 2.
        turn2 = list(p.recv())
        assert [e["type"] for e in turn2] == ["system", "assistant", "result"]
        assert turn2[-1]["result"] == "world"

        # Counter back to zero.
        assert not p.has_complete_turn()


def test_recv_side_effects_session_id_and_usage_in_consumer():
    """session_id and usage_total are updated in recv() (consumer), not the reader thread."""
    lines = _make_lines(_TURN_1)

    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()

        assert p.session_id is None  # not set until recv() processes the event
        list(p.recv())
        assert p.session_id == "sid-t1"
        assert p.usage_total.get("input_tokens") == 3
        assert p.usage_total.get("output_tokens") == 5


def test_recv_raises_dead_on_eof_without_result():
    """None sentinel from reader (EOF) before a result → ProviderDeadError."""
    lines = _make_lines([
        {"type": "system", "subtype": "init", "session_id": "sid"},
        {"type": "assistant", "message": {"content": []}},
        # No result frame → reader will put None (EOF sentinel).
    ])

    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()
        with pytest.raises(ProviderDeadError):
            list(p.recv())


# ── Test 2: off-by-one regression ────────────────────────────────────────────

_AUTO_TURN = [
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "wake up!"}]}},
    {"type": "result", "result": "autonomous wake"},
]

_REPLY_A = [
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "reply A"}]}},
    {"type": "result", "result": "reply A"},
]


def test_off_by_one_autonomous_then_reply_pairing():
    """Autonomous turn arrives before send(A); consumer drains autonomous
    turn first, then gets A's reply — no off-by-one skew."""
    lines = _make_lines(_AUTO_TURN, _REPLY_A)

    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = _make_fake_popen(lines)
        p = _provider()
        p.spawn()

        # Drain autonomous turn first.
        auto_events = list(p.recv())
        auto_result = next(e for e in auto_events if e.get("type") == "result")
        assert auto_result["result"] == "autonomous wake"

        # Second turn (A's reply) should be buffered.
        assert p.has_complete_turn()

        # Drain A's reply.
        a_events = list(p.recv())
        a_result = next(e for e in a_events if e.get("type") == "result")
        assert a_result["result"] == "reply A"

        assert not p.has_complete_turn()
