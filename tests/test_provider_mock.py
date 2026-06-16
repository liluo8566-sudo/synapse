from __future__ import annotations

import pytest

from synapse_core.providers.mock import EchoProvider


def test_echo_three_event_sequence():
    p = EchoProvider()
    p.spawn()
    p.send("hi")
    events = list(p.recv())
    assert len(events) == 3
    assert events[0]["type"] == "system"
    assert events[0]["subtype"] == "init"
    assert events[1]["type"] == "assistant"
    assert events[2]["type"] == "result"
    assert events[2]["result"] == "echo: hi"
    assert p.session_id == "mock-sid-0001"
    assert p.usage_total == {"input_tokens": 10, "output_tokens": 5}
    p.close()
    assert p.alive is False


def test_echo_recv_stops_cleanly_after_result():
    p = EchoProvider()
    p.spawn()
    p.send("ping")
    it = p.recv()
    seen = []
    for ev in it:
        seen.append(ev)
    assert seen[-1]["type"] == "result"
    # Generator is now exhausted.
    with pytest.raises(StopIteration):
        next(it)
    p.close()


def test_echo_two_consecutive_turns():
    p = EchoProvider()
    p.spawn()
    p.send("turn1")
    out1 = list(p.recv())
    assert out1[-1]["result"] == "echo: turn1"
    p.send("turn2")
    out2 = list(p.recv())
    assert out2[-1]["result"] == "echo: turn2"
    # Usage accumulates across turns.
    assert p.usage_total == {"input_tokens": 20, "output_tokens": 10}
    p.close()
