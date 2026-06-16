"""Tests for InboundBuffer: trailing hold marker and prepend."""

from __future__ import annotations

from synapse_core.debounce import InboundBuffer


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


# ── trailing hold marker ──────────────────────────────────────────────────────


def test_trailing_ellipsis_unicode_upgrades_window() -> None:
    """Single U+2026 (…) at end upgrades to 10s hold window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等我想想…")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_trailing_double_ellipsis_unicode_upgrades_window() -> None:
    """Two consecutive U+2026 (……) at end upgrades to 10s hold window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("稍等……")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_trailing_ascii_dots_upgrades_window() -> None:
    """Three ascii dots (...) at end upgrades to 10s hold window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等我查一下...")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_trailing_more_ascii_dots_upgrades_window() -> None:
    """Five ascii dots (.....): still upgrades."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("hmm.....")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_trailing_cjk_dots_upgrades_window() -> None:
    """Three CJK full stops (。。。) at end upgrades to 10s hold window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等我。。。")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_two_ascii_dots_does_not_upgrade() -> None:
    """Two ascii dots (..) do NOT trigger trailing hold — below threshold."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("ok..")
    clock.advance(5.1)
    assert buf.ready()  # default 5s window, not 10s


def test_two_cjk_dots_does_not_upgrade() -> None:
    """Two CJK full stops (。。) do NOT trigger trailing hold."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("好。。")
    clock.advance(5.1)
    assert buf.ready()


def test_ellipsis_in_middle_only_does_not_upgrade() -> None:
    """Ellipsis in the middle with normal ending does NOT upgrade window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("我...觉得")  # ends with a character, not dots
    clock.advance(5.1)
    assert buf.ready()  # default 5s window applied


def test_ellipsis_in_middle_cjk_does_not_upgrade() -> None:
    """CJK ellipsis mid-text, normal ending: no upgrade."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("嗯……好吧")  # ends with a normal character
    clock.advance(5.1)
    assert buf.ready()


def test_trailing_fullwidth_tilde_upgrades_window() -> None:
    """Trailing full-width ～ (U+FF5E) upgrades to 10s hold window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("等我一下～")
    clock.advance(6.0)
    assert not buf.ready()
    clock.advance(4.1)
    assert buf.ready()


def test_trailing_halfwidth_tilde_does_not_upgrade() -> None:
    """Half-width ~ is NOT a hold marker — only the CN full-width ～."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("ok~")
    clock.advance(5.1)
    assert buf.ready()


def test_tilde_in_middle_does_not_upgrade() -> None:
    """～ mid-text with normal ending does NOT upgrade window."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("好～明天再说")
    clock.advance(5.1)
    assert buf.ready()


def test_trailing_comma_does_not_upgrade() -> None:
    """Trailing comma is NOT a hold marker (rejected candidate)."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("我想说，")
    clock.advance(5.1)
    assert buf.ready()


def test_trailing_hold_is_sticky_with_later_normal_bubble() -> None:
    """Trailing hold sets the upgrade; a later normal bubble does not shrink it."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("思考中……")
    clock.advance(2.0)
    buf.add("对")  # normal bubble, but hold is already sticky
    clock.advance(6.0)
    assert not buf.ready()  # only 6s since last bubble, hold window = 10s
    clock.advance(4.1)
    assert buf.ready()


# ── prepend ───────────────────────────────────────────────────────────────────


def test_prepend_text_appears_first_in_flush() -> None:
    """prepend inserts at front; flush returns prepended text then live bubbles."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("live bubble")
    buf.prepend("old body")
    out = buf.flush()
    assert out == "old body\nlive bubble"


def test_prepend_on_empty_buffer_then_add_and_flush() -> None:
    """prepend works on an empty buffer; later add appends after it."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.prepend("prepended")
    buf.add("new")
    out = buf.flush()
    assert out == "prepended\nnew"


def test_prepend_multiline_text_joins_correctly() -> None:
    """Multi-line prepended text is one element joined at flush."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("bubble")
    buf.prepend("line1\nline2")
    out = buf.flush()
    assert out == "line1\nline2\nbubble"


def test_prepend_empty_string_is_noop() -> None:
    """prepend('') must not add any element."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("only")
    buf.prepend("")
    assert len(buf) == 1
    assert buf.flush() == "only"


def test_prepend_whitespace_only_is_noop() -> None:
    """prepend with only whitespace is a no-op."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("only")
    buf.prepend("   ")
    assert len(buf) == 1
    assert buf.flush() == "only"


def test_prepend_does_not_affect_ready_timing() -> None:
    """prepend does NOT reset or alter _last_ts — ready() window unchanged."""
    clock = FakeClock()
    buf = InboundBuffer(clock=clock)
    buf.add("live")
    # Not ready yet (0s elapsed).
    assert not buf.ready()
    # prepend old body — timestamps should stay as-is.
    buf.prepend("old body")
    assert not buf.ready()
    clock.advance(5.1)
    assert buf.ready()
