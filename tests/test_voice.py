"""Tests for /voice <cn|en> — swap user-facing ack style.

cn (default) = 中文搞笑; en = English short. State persists to bridge_state.json
via the `persist_state` closure so a bridge crash does not silently revert.
"""

from __future__ import annotations

from synapse_core.commands import messages
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


def _noop(*_a, **_k) -> None:
    return None


def _make(state: BridgeState | None = None, *, persist=None):
    s = state if state is not None else BridgeState()
    ctx = CommandContext(
        state=s,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        persist_state=persist or (lambda: None),
    )
    return Registry(ctx), s


def test_voice_default_is_cn() -> None:
    _, s = _make()
    assert s.voice_style == "cn"


def test_voice_no_arg_renders_usage_in_current_style() -> None:
    reg, s = _make()
    verdict, reply = reg.dispatch("/voice")
    assert verdict == "handled"
    assert reply == messages.t("voice.usage", "cn", x="cn")


def test_voice_set_en_swaps_style_and_persists() -> None:
    persisted: list[int] = []
    reg, s = _make(persist=lambda: persisted.append(1))
    verdict, reply = reg.dispatch("/voice en")
    assert verdict == "handled"
    # Ack renders in the NEW style so the user sees a sample.
    assert reply == messages.t("voice.set", "en")
    assert s.voice_style == "en"
    assert persisted == [1]


def test_voice_set_cn_from_en() -> None:
    s = BridgeState()
    s.voice_style = "en"
    reg, s = _make(s)
    verdict, reply = reg.dispatch("/voice cn")
    assert verdict == "handled"
    assert reply == messages.t("voice.set", "cn")
    assert s.voice_style == "cn"


def test_voice_set_same_style_returns_already() -> None:
    reg, s = _make()
    verdict, reply = reg.dispatch("/voice cn")
    assert verdict == "handled"
    assert reply == messages.t("voice.same", "cn", x="cn")
    assert s.voice_style == "cn"


def test_voice_bad_arg_returns_usage() -> None:
    reg, s = _make()
    verdict, reply = reg.dispatch("/voice spanish")
    assert verdict == "handled"
    assert reply == messages.t("voice.usage", "cn", x="cn")
    assert s.voice_style == "cn"


def test_voice_swap_affects_subsequent_acks() -> None:
    """Live verification: after /voice en, /stop ack lands in English."""
    reg, s = _make()
    reg.dispatch("/voice en")
    _, stop_reply = reg.dispatch("/stop")
    assert stop_reply == "Stopped, session kept"
    reg.dispatch("/voice cn")
    _, stop_reply2 = reg.dispatch("/stop")
    assert stop_reply2 == "🛑施法已打断"
