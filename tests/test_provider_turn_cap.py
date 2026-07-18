"""Per-turn OUTPUT token cap tests.

Reuses the fake-subprocess approach from test_provider_idle.py: a scripted
stdout pipe yields stream-json lines with controlled usage figures so the
provider's per-turn output accumulator can be exercised without a real cc.
Idle thresholds are set generous so only the cap logic is under test.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from synapse_core.providers.cc import ClaudeCodeProvider


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _assistant(
    output_tokens: int,
    request_id: str | None = None,
    parent_tool_use_id: str | None = None,
    **usage,
) -> str:
    ev: dict = {
        "type": "assistant",
        "message": {"content": [], "usage": {"output_tokens": output_tokens, **usage}},
        # Real stream-json always carries this key (None for main-line, a
        # toolu_* id for subagent-attributed events) — verified empirically
        # against a real transcript (claude 2.1.197), so tests default it too.
        "parent_tool_use_id": parent_tool_use_id,
    }
    if request_id is not None:
        ev["request_id"] = request_id
    return _line(ev)


def _result(text: str = "ok") -> str:
    return _line({"type": "result", "result": text})


def _provider(lines: list[str], *, turn_output_cap: int = 30000):
    """Spawn a provider whose stdout replays `lines` immediately (no delays).

    Idle thresholds are large so the idle path never fires — only the output
    cap is under test.
    """
    p = ClaudeCodeProvider(
        channel="test",
        cwd="/tmp",
        stderr_log=None,
        idle_soft_s=30.0,
        idle_hard_s=60.0,
        turn_output_cap=turn_output_cap,
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


def test_normal_turn_under_cap_no_interrupt():
    """Output well under the cap completes normally, no cancel, flag stays off."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant(500, request_id="req-1"),
        _result(),
    ]
    p, fake = _provider(lines, turn_output_cap=30000)
    out = list(p.recv())
    assert out[-1]["type"] == "result"
    assert p.turn_output_capped is False
    fake.terminate.assert_not_called()


def test_repeated_usage_same_request_not_double_counted():
    """A request's usage line repeated many times must count once (max), so a
    per-line value under the cap never trips even when repeated N times."""
    # 5 identical lines of 10000 each under one request_id. Naive summing would
    # be 50000 > cap; correct dedup keeps 10000 (max) < cap.
    lines = [_line({"type": "system", "subtype": "init", "session_id": "s"})]
    lines += [_assistant(10000, request_id="req-1") for _ in range(5)]
    lines.append(_result())
    p, fake = _provider(lines, turn_output_cap=30000)
    out = list(p.recv())
    assert out[-1]["type"] == "result"
    assert p.turn_output_capped is False
    fake.terminate.assert_not_called()


def test_multi_request_turn_sums_and_triggers_cancel():
    """A turn spanning several request_ids sums the per-request max; exceeding
    the cap sets the flag and calls cancel()."""
    # Three tool round-trips: 12k + 12k + 12k = 36k > cap(30k). Each request's
    # usage line also repeats once to prove dedup + sum coexist.
    lines = [_line({"type": "system", "subtype": "init", "session_id": "s"})]
    for req in ("req-1", "req-2", "req-3"):
        lines.append(_assistant(12000, request_id=req))
        lines.append(_assistant(12000, request_id=req))  # repeat, same request
    lines.append(_result())  # cc would still be streaming; cancel cuts it off
    p, fake = _provider(lines, turn_output_cap=30000)
    out = list(p.recv())
    # Breach happens on the third request's assistant event; recv returns
    # cleanly after cancel — NO ProviderDeadError raised (no retry path).
    assert p.turn_output_capped is True
    # The breaching assistant event was still yielded; no result reached.
    assert out[-1]["type"] == "assistant"
    assert all(e["type"] != "result" for e in out)
    fake.terminate.assert_called_once()


def test_huge_input_cache_does_not_trigger():
    """Massive input_tokens / cache_read / cache_creation must NOT count —
    only output_tokens. A 200k window with tiny output stays under the cap."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant(
            100,
            request_id="req-1",
            input_tokens=150000,
            cache_read_input_tokens=200000,
            cache_creation_input_tokens=50000,
        ),
        _result(),
    ]
    p, fake = _provider(lines, turn_output_cap=30000)
    out = list(p.recv())
    assert out[-1]["type"] == "result"
    assert p.turn_output_capped is False
    fake.terminate.assert_not_called()


def test_cap_zero_disables():
    """turn_output_cap=0 disables the brake: even huge output never trips."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant(500000, request_id="req-1"),
        _result(),
    ]
    p, fake = _provider(lines, turn_output_cap=0)
    out = list(p.recv())
    assert out[-1]["type"] == "result"
    assert p.turn_output_capped is False
    fake.terminate.assert_not_called()


def test_subagent_output_excluded_even_if_huge():
    """Assistant events attributed to a Task-dispatched subagent (carrying a
    non-None parent_tool_use_id) must NOT count toward the cap, however large
    their output_tokens — an agent-dispatch turn must not false-trigger."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant(
            500000, request_id="req-sub", parent_tool_use_id="toolu_01SUBAGENT"
        ),
        _result(),
    ]
    p, fake = _provider(lines, turn_output_cap=20000)
    out = list(p.recv())
    assert out[-1]["type"] == "result"
    assert p.turn_output_capped is False
    fake.terminate.assert_not_called()


def test_mainline_output_still_triggers_alongside_subagent_traffic():
    """Main-line events (parent_tool_use_id=None) still trigger the cap even
    when interleaved with huge, correctly-excluded subagent output."""
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        # Huge subagent output — excluded, must not contribute to the sum.
        _assistant(
            500000, request_id="req-sub", parent_tool_use_id="toolu_01SUBAGENT"
        ),
        # Main-line output alone breaches the cap.
        _assistant(25000, request_id="req-main"),
        _result(),
    ]
    p, fake = _provider(lines, turn_output_cap=20000)
    out = list(p.recv())
    assert p.turn_output_capped is True
    assert out[-1]["type"] == "assistant"
    assert all(e["type"] != "result" for e in out)
    fake.terminate.assert_called_once()


def _rescript(p, fake, lines: list[str]) -> None:
    """Feed a fresh scripted turn into the provider's event queue, as if the
    resident reader had consumed a new turn's stream. Drains leftovers first
    (incl. the EOF sentinel from the exhausted first scripted stream)."""
    import queue as _queue

    try:
        while True:
            p._event_queue.get_nowait()
    except _queue.Empty:
        pass
    for ln in lines:
        ev = json.loads(ln)
        if ev.get("type") == "result":
            with p._turn_lock:
                p._complete_turns += 1
        p._event_queue.put(ev)


def test_counter_resets_each_turn():
    """The per-turn counter resets at the start of every recv(): a first turn
    near the cap does not carry over into the next turn."""
    turn1 = [
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _assistant(20000, request_id="req-1"),
        _result(),
    ]
    p, fake = _provider(turn1, turn_output_cap=30000)
    list(p.recv())
    assert p.turn_output_capped is False
    # Second turn under the cap on its own. If the counter had carried over
    # (20000 + 20000 = 40000) it would trip; a correct reset keeps it at 20000.
    _rescript(p, fake, [_assistant(20000, request_id="req-2"), _result()])
    list(p.recv())
    assert p.turn_output_capped is False
