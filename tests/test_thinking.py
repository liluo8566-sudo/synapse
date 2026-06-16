"""Tests for E-polish /thinking on|off.

When `state.thinking_on` is True, the bridge:
- collects cc stream-json `thinking` content blocks into one string,
- emits ONE WeChat bubble per turn prefixed `【思考】<text>`,
- truncates >100 chars with `…`,
- still emits the regular assistant bubbles after the thinking bubble.

Default is off.
"""

from __future__ import annotations

from synapse_core.commands.registry import CommandContext, Registry
from synapse_wx.split import format_thinking_bubbles
from synapse_core.state import BridgeState


def _noop(*_a, **_k) -> None:
    return None


def _make_registry(state: BridgeState | None = None) -> tuple[Registry, BridgeState]:
    s = state if state is not None else BridgeState()
    ctx = CommandContext(
        state=s,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
    )
    return Registry(ctx), s


# ── /thinking dispatch ───────────────────────────────────────────────


def test_thinking_default_off() -> None:
    _, s = _make_registry()
    assert s.thinking_on is False


def test_thinking_on_flips_state() -> None:
    reg, s = _make_registry()
    verdict, reply = reg.dispatch("/thinking on")
    assert verdict == "handled"
    assert reply is not None
    assert "偷窥" in reply
    assert s.thinking_on is True


def test_thinking_off_flips_state() -> None:
    s = BridgeState()
    s.thinking_on = True
    reg, s = _make_registry(s)
    verdict, reply = reg.dispatch("/thinking off")
    assert verdict == "handled"
    assert reply is not None
    assert "不看" in reply
    assert s.thinking_on is False


def test_thinking_no_arg_returns_usage() -> None:
    reg, _ = _make_registry()
    verdict, reply = reg.dispatch("/thinking")
    assert verdict == "handled"
    assert reply is not None
    assert "on" in reply.lower() and "off" in reply.lower()


def test_thinking_bad_arg_returns_error() -> None:
    reg, s = _make_registry()
    verdict, reply = reg.dispatch("/thinking maybe")
    assert verdict == "handled"
    assert reply is not None
    assert s.thinking_on is False


# ── format_thinking_bubbles (split.py helper) ────────────────────────


def test_format_thinking_short_single_bubble() -> None:
    out = format_thinking_bubbles("我在想这个问题")
    assert out == ["🧠我在想这个问题"]


def test_format_thinking_long_text_kept_whole() -> None:
    # Single-bubble mode (test-drive): no splitting, no truncation.
    body = "。".join([f"第{i}个想法" for i in range(1, 31)]) + "。"
    out = format_thinking_bubbles(body)
    assert len(out) == 1
    assert out[0].startswith("🧠")
    assert out[0] == f"🧠{body}"


def test_format_thinking_empty_returns_empty_list() -> None:
    assert format_thinking_bubbles("") == []
    assert format_thinking_bubbles("   \n  ") == []
    assert format_thinking_bubbles(None) == []
