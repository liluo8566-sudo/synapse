from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot, Message

logger = logging.getLogger(__name__)

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # 20 MB

_READ_TOOL_PREFIX = "Use the Read tool to view: "


def _uid() -> str:
    return uuid.uuid4().hex[:12]


async def download_tg_file(bot: "Bot", file_id: str, dest_dir: Path, suffix: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    tg_file = await bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > TG_DOWNLOAD_LIMIT:
        logger.warning("download_tg_file: file %s is %d bytes, exceeds 20MB TG limit", file_id, tg_file.file_size)
    dest = dest_dir / f"{_uid()}{suffix}"
    await tg_file.download_to_drive(dest)
    return dest


async def materialize_photo(bot: "Bot", message: "Message", data_dir: Path) -> list[Path]:
    if not message.photo:
        return []
    largest = max(message.photo, key=lambda p: p.file_size or 0)
    dest_dir = data_dir / "media" / "Images"
    path = await download_tg_file(bot, largest.file_id, dest_dir, ".jpg")
    return [path]


async def materialize_document(bot: "Bot", message: "Message", data_dir: Path) -> Path | None:
    doc = message.document
    if not doc:
        return None
    dest_dir = data_dir / "media" / "Documents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(doc.file_name).suffix if doc.file_name and "." in doc.file_name else ".bin"
    tg_file = await bot.get_file(doc.file_id)
    if tg_file.file_size and tg_file.file_size > TG_DOWNLOAD_LIMIT:
        logger.warning("materialize_document: file %s is %d bytes, exceeds 20MB TG limit", doc.file_id, tg_file.file_size)
    filename = doc.file_name if doc.file_name else f"{_uid()}{suffix}"
    dest = dest_dir / filename
    if dest.exists():
        dest = dest_dir / f"{_uid()}_{filename}"
    await tg_file.download_to_drive(dest)
    return dest


async def materialize_sticker(bot: "Bot", message: "Message", data_dir: Path) -> Path | None:
    sticker = message.sticker
    if not sticker:
        return None
    dest_dir = data_dir / "media" / "Stickers"
    path = await download_tg_file(bot, sticker.file_id, dest_dir, ".webp")
    return path


async def materialize_animation(bot: "Bot", message: "Message", data_dir: Path) -> Path | None:
    anim = message.animation
    if not anim:
        return None
    dest_dir = data_dir / "media" / "Images"
    suffix = Path(anim.file_name).suffix if anim.file_name and "." in anim.file_name else ".gif"
    path = await download_tg_file(bot, anim.file_id, dest_dir, suffix)
    return path


async def materialize_video(bot: "Bot", message: "Message", data_dir: Path) -> Path | None:
    video = message.video
    if not video:
        return None
    dest_dir = data_dir / "media" / "Videos"
    suffix = Path(video.file_name).suffix if video.file_name and "." in video.file_name else ".mp4"
    path = await download_tg_file(bot, video.file_id, dest_dir, suffix)
    return path


def build_read_instruction(paths: list[Path]) -> str:
    if not paths:
        return ""
    joined = ", ".join(str(p) for p in paths)
    return f"{_READ_TOOL_PREFIX}{joined}"
