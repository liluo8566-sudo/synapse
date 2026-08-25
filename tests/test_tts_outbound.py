"""Tests for kind='voice' in synapse_tg.media.outbound.send_media."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse_tg.media.outbound import send_media


@pytest.mark.asyncio
async def test_voice_calls_send_voice(tmp_path: Path) -> None:
    audio = tmp_path / "out.mp3"
    audio.write_bytes(b"fake-mp3")

    bot = MagicMock()
    bot.send_voice = AsyncMock(return_value=None)

    ok = await send_media(bot, chat_id=1, kind="voice", path=str(audio))
    assert ok is True
    assert bot.send_voice.call_count == 1
    call_kwargs = bot.send_voice.call_args.kwargs
    assert call_kwargs["chat_id"] == 1


@pytest.mark.asyncio
async def test_voice_does_not_call_send_photo(tmp_path: Path) -> None:
    audio = tmp_path / "out.mp3"
    audio.write_bytes(b"fake-mp3")

    bot = MagicMock()
    bot.send_voice = AsyncMock(return_value=None)
    bot.send_photo = AsyncMock(return_value=None)

    await send_media(bot, chat_id=2, kind="voice", path=str(audio))
    assert bot.send_photo.call_count == 0
