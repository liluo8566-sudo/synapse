"""GIF routing: small gifs → send_photo; large gifs → send_animation."""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse_tg.media.outbound import GIF_PHOTO_MAX_SIDE, _gif_dimensions, send_media


def _make_gif_bytes(width: int, height: int) -> bytes:
    header = b"GIF89a"
    header += struct.pack("<HH", width, height)
    return header  # 10 bytes, enough for _gif_dimensions


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_animation = AsyncMock()
    return bot


# --- _gif_dimensions unit tests -------------------------------------------


def test_gif_dimensions_small(tmp_path: Path) -> None:
    p = tmp_path / "small.gif"
    p.write_bytes(_make_gif_bytes(240, 240))
    assert _gif_dimensions(str(p)) == (240, 240)


def test_gif_dimensions_large(tmp_path: Path) -> None:
    p = tmp_path / "large.gif"
    p.write_bytes(_make_gif_bytes(480, 360))
    assert _gif_dimensions(str(p)) == (480, 360)


def test_gif_dimensions_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "bad.gif"
    p.write_bytes(b"\x00\x01\x02\x03\x04")
    assert _gif_dimensions(str(p)) is None


def test_gif_dimensions_wrong_magic(tmp_path: Path) -> None:
    p = tmp_path / "notgif.gif"
    p.write_bytes(b"PNG\r\n\x1a\n" + b"\x00" * 10)
    assert _gif_dimensions(str(p)) is None


def test_gif_dimensions_missing_file() -> None:
    assert _gif_dimensions("/nonexistent/path.gif") is None


# --- send_media routing tests -----------------------------------------------


def test_small_gif_routes_to_send_photo(tmp_path: Path) -> None:
    """Small gif (max side < GIF_PHOTO_MAX_SIDE) → bot.send_photo, not send_animation."""
    p = tmp_path / "small.gif"
    p.write_bytes(_make_gif_bytes(240, 240))
    assert max(240, 240) < GIF_PHOTO_MAX_SIDE

    bot = _make_bot()
    ok = asyncio.run(send_media(bot, 1, "gif", str(p)))

    assert ok is True
    bot.send_photo.assert_called_once()
    bot.send_animation.assert_not_called()


def test_large_gif_routes_to_send_animation(tmp_path: Path) -> None:
    """Large gif (max side >= GIF_PHOTO_MAX_SIDE) → bot.send_animation."""
    p = tmp_path / "large.gif"
    p.write_bytes(_make_gif_bytes(480, 360))
    assert max(480, 360) >= GIF_PHOTO_MAX_SIDE

    bot = _make_bot()
    ok = asyncio.run(send_media(bot, 1, "gif", str(p)))

    assert ok is True
    bot.send_animation.assert_called_once()
    bot.send_photo.assert_not_called()


def test_corrupt_gif_falls_back_to_send_animation(tmp_path: Path) -> None:
    """Non-GIF bytes with kind 'gif' → dims None → send_animation (safe fallback)."""
    p = tmp_path / "corrupt.gif"
    p.write_bytes(b"not a gif at all")

    bot = _make_bot()
    ok = asyncio.run(send_media(bot, 1, "gif", str(p)))

    assert ok is True
    bot.send_animation.assert_called_once()
    bot.send_photo.assert_not_called()


def test_gif_exactly_at_threshold_routes_to_send_animation(tmp_path: Path) -> None:
    """Gif with max side == GIF_PHOTO_MAX_SIDE is NOT small → send_animation."""
    p = tmp_path / "boundary.gif"
    p.write_bytes(_make_gif_bytes(GIF_PHOTO_MAX_SIDE, 100))

    bot = _make_bot()
    asyncio.run(send_media(bot, 1, "gif", str(p)))

    bot.send_animation.assert_called_once()
    bot.send_photo.assert_not_called()
