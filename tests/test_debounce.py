"""Tests for InboundBuffer: quiet window, hold words, exact-match semantics."""

from __future__ import annotations

from synapse_core.debounce import InboundBuffer


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


def test_empty_buffer_state() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    assert len(buf) == 0
    assert not buf
    assert not buf.ready()


def test_one_bubble_ready_after_quiet_window() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("hello")
    assert len(buf) == 1
    assert bool(buf)
    assert not buf.ready()
    clock.advance(5.1)
    assert buf.ready()


def test_two_bubbles_within_window_extend_wait() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("one")
    clock.advance(3.0)
    buf.add("two")
    # Only 3s since second bubble — not ready.
    clock.advance(3.0)
    assert not buf.ready()
    # 5s after the second bubble — ready.
    clock.advance(2.1)
    assert buf.ready()


def test_hold_word_extends_window_to_ten_seconds() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_hold_word_then_normal_keeps_extended_window() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("稍等")
    clock.advance(2.0)
    buf.add("我查一下")  # later normal bubble shouldn't shrink window
    clock.advance(6.0)
    assert not buf.ready()  # only 6s since latest bubble, hold window = 10s
    clock.advance(4.1)
    assert buf.ready()


def test_hold_substring_does_not_trigger() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等下")  # contains "等" but not exact match
    clock.advance(5.1)
    assert buf.ready()  # default 5s window applied


def test_flush_returns_joined_and_clears() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("first")
    buf.add("second")
    out = buf.flush()
    assert out == "first\nsecond"
    assert len(buf) == 0
    assert not buf
    assert not buf.ready()


def test_empty_text_dropped() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("")
    buf.add("   ")
    assert len(buf) == 0


def test_window_resets_after_flush() -> None:
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等")
    clock.advance(10.1)
    buf.flush()
    buf.add("normal")
    clock.advance(5.1)
    # After flush, hold-window must reset to default 5s.
    assert buf.ready()
