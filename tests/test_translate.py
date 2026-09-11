"""Tests for synapse_core.translate (thinking-bubble translation)."""

from __future__ import annotations

import sys

import pytest

from synapse_core.translate import needs_translation, translate, translate_sync
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_wx.config import Config as WxConfig
from synapse_wx.loop import MainLoop as WxLoop


# ── needs_translation ────────────────────────────────────────────────


def test_needs_translation_pure_english_true() -> None:
    assert needs_translation("This is pure English text.", "zh") is True


def test_needs_translation_pure_chinese_false() -> None:
    assert needs_translation("这是一段纯中文文本，不需要翻译。", "zh") is False


def test_needs_translation_mixed_half_false() -> None:
    text = "abcde" + "中文字符"  # 5 latin letters, 4 CJK -> 4/9 >= 0.3
    assert needs_translation(text, "zh") is False


def test_needs_translation_empty_false() -> None:
    assert needs_translation("", "zh") is False


def test_needs_translation_emoji_only_false() -> None:
    assert needs_translation("😀🎉🚀", "zh") is False


def test_needs_translation_non_zh_target_true_unless_empty() -> None:
    assert needs_translation("Bonjour", "fr") is True
    assert needs_translation("", "fr") is False


# ── translate() ───────────────────────────────────────────────────────

_UPPER_CMD = [
    sys.executable, "-c",
    "import sys; print(sys.stdin.read().split('\\n\\n',1)[1].upper())",
]
_FAIL_CMD = [sys.executable, "-c", "import sys; sys.exit(1)"]
_SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(5)"]


@pytest.mark.asyncio
async def test_translate_returns_transformed_text() -> None:
    result = await translate("hello world", "zh", _UPPER_CMD)
    assert result == "HELLO WORLD"


@pytest.mark.asyncio
async def test_translate_failing_command_returns_none() -> None:
    result = await translate("hello", "zh", _FAIL_CMD)
    assert result is None


@pytest.mark.asyncio
async def test_translate_timeout_returns_none_quickly() -> None:
    import time
    start = time.monotonic()
    result = await translate("hello", "zh", _SLEEP_CMD, timeout=0.2)
    elapsed = time.monotonic() - start
    assert result is None
    assert elapsed < 3.0


# ── translate_sync() ──────────────────────────────────────────────────


def test_translate_sync_returns_transformed_text() -> None:
    result = translate_sync("hello world", "zh", _UPPER_CMD)
    assert result == "HELLO WORLD"


def test_translate_sync_failing_command_returns_none() -> None:
    result = translate_sync("hello", "zh", _FAIL_CMD)
    assert result is None


def test_translate_sync_timeout_returns_none_quickly() -> None:
    import time
    start = time.monotonic()
    result = translate_sync("hello", "zh", _SLEEP_CMD, timeout=0.2)
    elapsed = time.monotonic() - start
    assert result is None
    assert elapsed < 3.0


# ── TgLoop._resolve_thinking_text ───────────────────────────────────


def _loop(tmp_path, **cfg_kwargs):
    cfg = TgConfig(data_dir=tmp_path / "tg-data", **cfg_kwargs)
    return TgLoop(cfg, alerts=None)


@pytest.mark.asyncio
async def test_resolve_thinking_text_substitutes_translation(tmp_path, monkeypatch) -> None:
    loop = _loop(tmp_path, thinking_translate_to="zh")

    async def fake_translate(text, target, cmd, timeout=45.0):
        return f"[translated:{target}] {text}"

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    out = await loop._resolve_thinking_text("I am thinking about this.")
    assert out == "[translated:zh] I am thinking about this."


@pytest.mark.asyncio
async def test_resolve_thinking_text_falls_back_on_none(tmp_path, monkeypatch) -> None:
    loop = _loop(tmp_path, thinking_translate_to="zh")

    async def fake_translate(text, target, cmd, timeout=45.0):
        return None

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    original = "I am thinking about this."
    out = await loop._resolve_thinking_text(original)
    assert out == original


@pytest.mark.asyncio
async def test_resolve_thinking_text_off_by_default(tmp_path) -> None:
    loop = _loop(tmp_path)
    original = "I am thinking about this."
    out = await loop._resolve_thinking_text(original)
    assert out == original


# ── MainLoop (wx)._resolve_thinking_text ────────────────────────────


def _wx_loop(tmp_path, **cfg_kwargs) -> WxLoop:
    from synapse_core.debounce import InboundBuffer
    from synapse_core.sessionend.tracker import SessionTracker
    from synapse_core.state import BridgeState

    cfg = WxConfig(**cfg_kwargs)
    return WxLoop(
        ilink=None,
        provider_factory=lambda *_a, **_k: None,
        state=BridgeState(),
        sessions=SessionTracker(state_path=tmp_path / "sessions.json"),
        buffer=InboundBuffer(),
        alert_dir=tmp_path / "alerts",
        cfg=cfg,
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        media_dir=tmp_path / "media",
    )


def test_wx_resolve_thinking_text_substitutes_translation(tmp_path, monkeypatch) -> None:
    loop = _wx_loop(tmp_path, thinking_translate_to="zh")

    def fake_translate_sync(text, target, cmd, timeout=45.0):
        return f"[translated:{target}] {text}"

    monkeypatch.setattr("synapse_wx.loop.translate_sync", fake_translate_sync)
    out = loop._resolve_thinking_text("I am thinking about this.")
    assert out == "[translated:zh] I am thinking about this."


def test_wx_resolve_thinking_text_falls_back_on_none(tmp_path, monkeypatch) -> None:
    loop = _wx_loop(tmp_path, thinking_translate_to="zh")

    def fake_translate_sync(text, target, cmd, timeout=45.0):
        return None

    monkeypatch.setattr("synapse_wx.loop.translate_sync", fake_translate_sync)
    original = "I am thinking about this."
    out = loop._resolve_thinking_text(original)
    assert out == original


def test_wx_resolve_thinking_text_off_by_default(tmp_path) -> None:
    loop = _wx_loop(tmp_path)
    original = "I am thinking about this."
    out = loop._resolve_thinking_text(original)
    assert out == original
