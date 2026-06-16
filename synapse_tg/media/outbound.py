from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

TG_SEND_LIMIT = 50 * 1024 * 1024  # 50 MB


async def send_media(
    bot: "Bot",
    chat_id: int,
    kind: str,
    path: str,
    reply_to: int | None = None,
) -> None:
    src = Path(path)
    if not src.exists():
        logger.error("send_media: file not found: %s", path)
        return

    size = src.stat().st_size
    if size > TG_SEND_LIMIT:
        logger.warning("send_media: file %s is %d bytes, exceeds 50MB TG send limit", path, size)

    kwargs: dict = {"chat_id": chat_id}
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to

    try:
        with open(path, "rb") as fh:
            if kind == "image":
                await bot.send_photo(**kwargs, photo=fh)
            elif kind == "gif":
                await bot.send_animation(**kwargs, animation=fh)
            elif kind == "video":
                await bot.send_video(**kwargs, video=fh)
            elif kind == "file":
                await bot.send_document(**kwargs, document=fh)
            elif kind == "sticker":
                await bot.send_sticker(**kwargs, sticker=fh)
            else:
                logger.error("send_media: unknown kind %r for path %s", kind, path)
    except Exception as e:
        logger.error("send_media: TG API error sending %s (%s): %s", path, kind, e)
