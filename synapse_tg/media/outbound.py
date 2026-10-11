from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from telegram.error import BadRequest, RetryAfter

if TYPE_CHECKING:
    from telegram import Bot, Message

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
    spoiler: bool = False,
    effect_id: str | None = None,
) -> Message | None:
    """Send one media bubble. Returns the sent Message on success, None on
    failure.

    A None return lets the caller log and continue with remaining bubbles;
    media loss should not kill the text flow.

    `spoiler`: blurs the media until tapped — only photo/animation/video
    (incl. small-gif-as-photo) support it; ignored for file/sticker/voice.
    `effect_id`: Bot API message_effect_id, private chats only (caller's
    concern); a BadRequest with it set is retried once without it so a bad
    effect never loses the bubble.
    """
    src = Path(path)
    if not src.exists():
        logger.error("send_media: file not found: %s", path)
        return None

    size = src.stat().st_size
    if size > TG_SEND_LIMIT:
        logger.warning("send_media: file %s is %d bytes, exceeds 50MB TG send limit", path, size)

    kwargs: dict = {"chat_id": chat_id}
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to
    if effect_id is not None:
        kwargs["message_effect_id"] = effect_id

    gif_as_photo = False
    if kind == "gif":
        dims = _gif_dimensions(path)
        if dims is not None and max(dims) < GIF_PHOTO_MAX_SIDE:
            gif_as_photo = True

    if spoiler and kind in ("image", "gif", "video"):
        kwargs["has_spoiler"] = True

    async def _send_once(kw: dict) -> Message | None:
        with open(path, "rb") as fh:
            if kind == "image":
                return await bot.send_photo(**kw, photo=fh)
            if kind == "gif":
                if gif_as_photo:
                    return await bot.send_photo(**kw, photo=fh)
                return await bot.send_animation(**kw, animation=fh)
            if kind == "video":
                return await bot.send_video(**kw, video=fh)
            if kind == "file":
                return await bot.send_document(**kw, document=fh)
            if kind == "sticker":
                return await bot.send_sticker(**kw, sticker=fh)
            if kind == "voice":
                return await bot.send_voice(**kw, voice=fh)
            logger.error("send_media: unknown kind %r for path %s", kind, path)
            return None

    async def _attempt(kw: dict) -> Message | None:
        """RetryAfter-aware send; BadRequest/other errors propagate for the
        caller to handle (effect retry) or re-raise."""
        attempts = max(1, send_retry_max)
        for i in range(attempts):
            try:
                return await _send_once(kw)
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 0)) or 0.0
                if wait > retry_after_cap_sec or i == attempts - 1:
                    logger.error(
                        "send_media: RetryAfter %.1fs sending %s (%s); giving up", wait, path, kind
                    )
                    return None
                await asyncio.sleep(wait + _RETRY_AFTER_MARGIN_SEC)
        return None

    try:
        return await _attempt(kwargs)
    except BadRequest as e:
        if "message_effect_id" in kwargs:
            logger.warning(
                "send_media: BadRequest with effect, retrying without effect (%s): %s", kind, e
            )
            retry_kwargs = {k: v for k, v in kwargs.items() if k != "message_effect_id"}
            try:
                return await _attempt(retry_kwargs)
            except Exception as e2:
                logger.error(
                    "send_media: TG API error sending %s (%s) after dropping effect: %s",
                    path, kind, e2,
                )
                return None
        logger.error("send_media: TG API error sending %s (%s): %s", path, kind, e)
        return None
    except Exception as e:
        logger.error("send_media: TG API error sending %s (%s): %s", path, kind, e)
        return None
