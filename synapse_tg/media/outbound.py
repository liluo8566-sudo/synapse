from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from telegram.error import RetryAfter

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

TG_SEND_LIMIT = 50 * 1024 * 1024  # 50 MB
# Extra seconds added on top of a 429 RetryAfter before retrying the send.
_RETRY_AFTER_MARGIN_SEC = 0.5
# Gifs whose larger side is below this render tiny via sendAnimation; send as photo instead.
GIF_PHOTO_MAX_SIDE = 320


def _gif_dimensions(path: str) -> tuple[int, int] | None:
    """Return (width, height) parsed from GIF header, or None on any failure."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(10)
        if len(header) < 10:
            return None
        if header[:6] not in (b"GIF87a", b"GIF89a"):
            return None
        width, height = struct.unpack_from("<HH", header, 6)
        return width, height
    except Exception:
        return None


async def send_media(
    bot: "Bot",
    chat_id: int,
    kind: str,
    path: str,
    reply_to: int | None = None,
    *,
    send_retry_max: int = 2,
    retry_after_cap_sec: float = 60.0,
) -> bool:
    """Send one media bubble. Returns True on success, False on failure.

    A False return lets the caller log and continue with remaining bubbles;
    media loss should not kill the text flow.
    """
    src = Path(path)
    if not src.exists():
        logger.error("send_media: file not found: %s", path)
        return False

    size = src.stat().st_size
    if size > TG_SEND_LIMIT:
        logger.warning("send_media: file %s is %d bytes, exceeds 50MB TG send limit", path, size)

    kwargs: dict = {"chat_id": chat_id}
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to

    gif_as_photo = False
    if kind == "gif":
        dims = _gif_dimensions(path)
        if dims is not None and max(dims) < GIF_PHOTO_MAX_SIDE:
            gif_as_photo = True

    async def _send_once() -> bool:
        with open(path, "rb") as fh:
            if kind == "image":
                await bot.send_photo(**kwargs, photo=fh)
            elif kind == "gif":
                if gif_as_photo:
                    await bot.send_photo(**kwargs, photo=fh)
                else:
                    await bot.send_animation(**kwargs, animation=fh)
            elif kind == "video":
                await bot.send_video(**kwargs, video=fh)
            elif kind == "file":
                await bot.send_document(**kwargs, document=fh)
            elif kind == "sticker":
                await bot.send_sticker(**kwargs, sticker=fh)
            else:
                logger.error("send_media: unknown kind %r for path %s", kind, path)
                return False
        return True

    attempts = max(1, send_retry_max)
    for i in range(attempts):
        try:
            return await _send_once()
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 0)) or 0.0
            if wait > retry_after_cap_sec or i == attempts - 1:
                logger.error(
                    "send_media: RetryAfter %.1fs sending %s (%s); giving up", wait, path, kind
                )
                return False
            await asyncio.sleep(wait + _RETRY_AFTER_MARGIN_SEC)
        except Exception as e:
            logger.error("send_media: TG API error sending %s (%s): %s", path, kind, e)
            return False
    return False
