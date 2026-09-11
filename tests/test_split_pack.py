"""Tests for pack_for_tg (thinking-bubble splitting) and its use in _deliver_reply."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_tg.split import pack_for_tg


def test_empty_text_returns_empty_list() -> None:
    assert pack_for_tg("") == []


def test_short_text_is_single_element() -> None:
    text = "hello there"
    out = pack_for_tg(text, limit=3500)
    assert out == [text]


def test_five_paragraphs_pack_into_two_bubbles() -> None:
    paras = [f"para{i} " + ("x" * 990) for i in range(5)]
    text = "\n\n".join(paras)
    out = pack_for_tg(text, limit=3500)
    assert len(out) == 2
    for bubble in out:
        assert len(bubble) <= 3500
    joined = "\n\n".join(out)
    for i in range(5):
        assert f"para{i} " in joined
    # order preserved
    positions = [joined.index(f"para{i} ") for i in range(5)]
    assert positions == sorted(positions)


def test_single_long_line_no_punctuation_every_chunk_under_limit() -> None:
    text = "x" * 8000
    out = pack_for_tg(text, limit=3500)
    assert len(out) >= 3
    for bubble in out:
        assert len(bubble) <= 3500
    assert "".join(out) == text or len("".join(out)) == 8000


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> object:
        self.messages.append(kwargs)
        return type("Msg", (), {"message_id": len(self.messages)})()

    async def send_chat_action(self, **_kwargs) -> None:
        return None


def _make_loop(tmp_path: Path) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    return TgLoop(cfg)


@pytest.mark.asyncio
async def test_deliver_reply_splits_long_thinking_into_multiple_bubbles(
    tmp_path: Path,
) -> None:
    loop = _make_loop(tmp_path)
    loop._state.thinking_on = True
    bot = FakeBot()

    long_thinking = "\n\n".join(f"thought {i} " + ("y" * 990) for i in range(5))

    await loop._deliver_reply(bot, 42, "ok", long_thinking)

    think_msgs = [m for m in bot.messages if "\U0001f4ad" in m["text"]]
    assert len(think_msgs) >= 2
    for i, m in enumerate(think_msgs, start=1):
        assert f"({i}/{len(think_msgs)})" in m["text"]
        assert len(m["text"]) <= 3600  # limit + wrapper/HTML headroom


@pytest.mark.asyncio
async def test_deliver_reply_single_thinking_bubble_no_index(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop._state.thinking_on = True
    bot = FakeBot()

    await loop._deliver_reply(bot, 42, "ok", "short thought")

    think_msgs = [m for m in bot.messages if "\U0001f4ad" in m["text"]]
    assert len(think_msgs) == 1
    assert "(1/1)" not in think_msgs[0]["text"]
