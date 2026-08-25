"""Tests for tts.synthesize — mocked httpx."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse_tg import tts


class _FakeCfg:
    tts_api_key = "key-abc"
    tts_voice_id = "voice-xyz"
    tts_model_id = "eleven_multilingual_v2"
    tts_output_format = "mp3_44100_128"


def _mock_client(status: int, content: bytes = b"audio-bytes"):
    resp = MagicMock()
    resp.status_code = status
    resp.content = content

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_success_returns_path_with_bytes() -> None:
    audio = b"fake-mp3-audio"
    fake_client = _mock_client(200, audio)
    with patch("synapse_tg.tts.httpx.AsyncClient", return_value=fake_client):
        path = await tts.synthesize("hello", _FakeCfg())
    assert path is not None
    assert path.endswith(".mp3")
    with open(path, "rb") as fh:
        assert fh.read() == audio
    os.unlink(path)


@pytest.mark.asyncio
async def test_non_200_returns_none() -> None:
    fake_client = _mock_client(401, b"")
    with patch("synapse_tg.tts.httpx.AsyncClient", return_value=fake_client):
        path = await tts.synthesize("hello", _FakeCfg())
    assert path is None


@pytest.mark.asyncio
async def test_network_error_returns_none() -> None:
    import httpx as _httpx

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=_httpx.ConnectError("unreachable"))

    with patch("synapse_tg.tts.httpx.AsyncClient", return_value=client):
        path = await tts.synthesize("hello", _FakeCfg())
    assert path is None


@pytest.mark.asyncio
async def test_text_truncation_at_cap() -> None:
    long_text = "x" * (tts._TEXT_CAP + 100)
    posted_text: list[str] = []

    async def _fake_post(url, *, headers, json, **kw):
        posted_text.append(json["text"])
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"audio"
        return resp

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = _fake_post

    with patch("synapse_tg.tts.httpx.AsyncClient", return_value=client):
        path = await tts.synthesize(long_text, _FakeCfg())

    assert path is not None
    os.unlink(path)
    assert len(posted_text) == 1
    assert len(posted_text[0]) == tts._TEXT_CAP
