"""Tests for synapse_core.translate (thinking-bubble translation)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

from synapse_core.translate import (
    DEFAULT_SYSTEM_PROMPT,
    default_translate_cmd,
    looks_translated,
    needs_translation,
    translate,
    translate_sync,
)
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


# ── looks_translated ─────────────────────────────────────────────────


def test_looks_translated_chinese_true() -> None:
    assert looks_translated("这是一段纯中文文本，不需要翻译。", "zh-CN") is True


def test_looks_translated_english_refusal_false() -> None:
    assert looks_translated("I'm not able to help with this request.", "zh-CN") is False


def test_looks_translated_empty_false() -> None:
    assert looks_translated("", "zh") is False


def test_looks_translated_non_zh_target_non_empty_true() -> None:
    assert looks_translated("Bonjour tout le monde", "fr") is True


# ── default_translate_cmd ────────────────────────────────────────────


def test_default_translate_cmd_no_system_prompt() -> None:
    cmd = default_translate_cmd("claude", "haiku")
    assert "--system-prompt" not in cmd


def test_default_translate_cmd_with_system_prompt() -> None:
    cmd = default_translate_cmd("claude", "haiku", DEFAULT_SYSTEM_PROMPT)
    assert cmd[-2:] == ["--system-prompt", DEFAULT_SYSTEM_PROMPT]


# ── translate() ───────────────────────────────────────────────────────

_UPPER_CMD = [
    sys.executable, "-c",
    "import sys; print(sys.stdin.read().split('\\n\\n',1)[1].upper())",
]
_FAIL_CMD = [sys.executable, "-c", "import sys; sys.exit(1)"]
_SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(5)"]
_REFUSE_CMD = [
    sys.executable, "-c",
    "print(\"I'm not able to help with this request.\")",
]


@pytest.mark.asyncio
async def test_translate_returns_transformed_text() -> None:
    result = await translate("hello world", "fr", _UPPER_CMD)
    assert result == "HELLO WORLD"


@pytest.mark.asyncio
async def test_translate_failing_command_returns_none() -> None:
    result = await translate("hello", "zh", _FAIL_CMD)
    assert result is None


@pytest.mark.asyncio
async def test_translate_refusal_returns_none() -> None:
    result = await translate("hello", "zh-CN", _REFUSE_CMD)
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
    result = translate_sync("hello world", "fr", _UPPER_CMD)
    assert result == "HELLO WORLD"


def test_translate_sync_failing_command_returns_none() -> None:
    result = translate_sync("hello", "zh", _FAIL_CMD)
    assert result is None


def test_translate_sync_refusal_returns_none() -> None:
    result = translate_sync("hello", "zh-CN", _REFUSE_CMD)
    assert result is None


def test_translate_sync_timeout_returns_none_quickly() -> None:
    import time
    start = time.monotonic()
    result = translate_sync("hello", "zh", _SLEEP_CMD, timeout=0.2)
    elapsed = time.monotonic() - start
    assert result is None
    assert elapsed < 3.0


# ── TgConfig.thinking_translate_system_prompt loader ────────────────


def test_tg_config_system_prompt_default(tmp_path) -> None:
    from synapse_tg.config import load_config as tg_load_config

    p = tmp_path / "config.toml"
    p.write_text("[provider]\n")
    cfg = tg_load_config(p)
    assert cfg.thinking_translate_system_prompt == DEFAULT_SYSTEM_PROMPT


def test_tg_config_system_prompt_override(tmp_path) -> None:
    from synapse_tg.config import load_config as tg_load_config

    p = tmp_path / "config.toml"
    p.write_text('[provider]\nthinking_translate_system_prompt = "custom prompt"\n')
    cfg = tg_load_config(p)
    assert cfg.thinking_translate_system_prompt == "custom prompt"


def test_tg_config_system_prompt_empty_override(tmp_path) -> None:
    from synapse_tg.config import load_config as tg_load_config

    p = tmp_path / "config.toml"
    p.write_text('[provider]\nthinking_translate_system_prompt = ""\n')
    cfg = tg_load_config(p)
    assert cfg.thinking_translate_system_prompt == ""


# ── TgLoop._translate_thinking_in_place ──────────────────────────────


def _loop(tmp_path, **cfg_kwargs):
    cfg = TgConfig(data_dir=tmp_path / "tg-data", **cfg_kwargs)
    return TgLoop(cfg, alerts=None)


@pytest.mark.asyncio
async def test_translate_thinking_in_place_same_count_edits_only(tmp_path, monkeypatch) -> None:
    loop = _loop(tmp_path, thinking_translate_to="zh")
    monkeypatch.setattr("synapse_tg.loop.pack_for_tg", lambda text: ["a", "b"])

    async def fake_translate(text, target, cmd, timeout=45.0):
        return "translated"

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    bot = AsyncMock()
    await loop._translate_thinking_in_place(bot, 123, [10, 11], "original")

    assert bot.edit_message_text.await_count == 2
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_translate_thinking_in_place_fewer_bubbles_edits_and_deletes(
    tmp_path, monkeypatch
) -> None:
    loop = _loop(tmp_path, thinking_translate_to="zh")
    monkeypatch.setattr("synapse_tg.loop.pack_for_tg", lambda text: ["a"])

    async def fake_translate(text, target, cmd, timeout=45.0):
        return "translated"

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    bot = AsyncMock()
    await loop._translate_thinking_in_place(bot, 123, [10, 11, 12], "original")

    assert bot.edit_message_text.await_count == 1
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 10
    assert bot.delete_message.await_count == 2
    deleted_ids = {c.kwargs["message_id"] for c in bot.delete_message.await_args_list}
    assert deleted_ids == {11, 12}
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_translate_thinking_in_place_more_bubbles_edits_and_sends(
    tmp_path, monkeypatch
) -> None:
    loop = _loop(tmp_path, thinking_translate_to="zh")
    monkeypatch.setattr("synapse_tg.loop.pack_for_tg", lambda text: ["a", "b"])

    async def fake_translate(text, target, cmd, timeout=45.0):
        return "translated"

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    bot = AsyncMock()
    await loop._translate_thinking_in_place(bot, 123, [10], "original")

    assert bot.edit_message_text.await_count == 1
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 10
    bot.delete_message.assert_not_awaited()
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_translate_thinking_in_place_none_makes_no_bot_calls(tmp_path, monkeypatch) -> None:
    loop = _loop(tmp_path, thinking_translate_to="zh")

    async def fake_translate(text, target, cmd, timeout=45.0):
        return None

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    bot = AsyncMock()
    await loop._translate_thinking_in_place(bot, 123, [10, 11], "original")

    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()


# ── MainLoop (wx)._deliver_reply — translation ordering ──────────────


class _FakeILink:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, to_user_id, ctx_token, text, **_kwargs) -> bool:
        self.sent.append(text)
        return True


def _wx_loop(tmp_path, ilink=None, **cfg_kwargs) -> WxLoop:
    from synapse_core.debounce import InboundBuffer
    from synapse_core.sessionend.tracker import SessionTracker
    from synapse_core.state import BridgeState

    cfg = WxConfig(**cfg_kwargs)
    return WxLoop(
        ilink=ilink if ilink is not None else _FakeILink(),
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


def test_wx_deliver_reply_translation_configured_reply_then_translated_thinking(
    tmp_path, monkeypatch
) -> None:
    ilink = _FakeILink()
    loop = _wx_loop(tmp_path, ilink=ilink, thinking_translate_to="zh")
    loop.state.thinking_on = True

    def fake_translate_sync(text, target, cmd, timeout=45.0):
        return f"[translated:{target}] {text}"

    monkeypatch.setattr("synapse_wx.loop.translate_sync", fake_translate_sync)
    loop._deliver_reply("wxid-1", "ctx-1", "the reply", "the thinking")

    assert ilink.sent == ["the reply", "\U0001f4ad[translated:zh] the thinking"]


def test_wx_deliver_reply_translation_none_falls_back_reply_then_original(
    tmp_path, monkeypatch
) -> None:
    ilink = _FakeILink()
    loop = _wx_loop(tmp_path, ilink=ilink, thinking_translate_to="zh")
    loop.state.thinking_on = True

    def fake_translate_sync(text, target, cmd, timeout=45.0):
        return None

    monkeypatch.setattr("synapse_wx.loop.translate_sync", fake_translate_sync)
    loop._deliver_reply("wxid-1", "ctx-1", "the reply", "the thinking")

    assert ilink.sent == ["the reply", "\U0001f4adthe thinking"]


def test_wx_deliver_reply_translation_off_thinking_then_reply(tmp_path) -> None:
    ilink = _FakeILink()
    loop = _wx_loop(tmp_path, ilink=ilink)
    loop.state.thinking_on = True

    loop._deliver_reply("wxid-1", "ctx-1", "the reply", "the thinking")

    assert ilink.sent == ["\U0001f4adthe thinking", "the reply"]
