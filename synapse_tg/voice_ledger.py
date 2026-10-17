"""Persistent ledger of voice messages the bridge has sent via TTS, so a
forwarded copy of one can later be recognised as "our own voice" (see
loop.on_voice). JSONL, newest entry last.

Lookups/writes take the ledger file path explicitly rather than reading it
off config, so this module stays a plain function pair with no state of its
own. Never raises into the caller — this is a nice-to-have lookup, not a
dependency of the send/receive path.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Message

logger = logging.getLogger(__name__)

# Keep the newest this many entries; trim once the file grows past CAP+SLACK
# so a normal chat cadence doesn't rewrite the file on every single send.
_CAP = 2000
_TRIM_AT = _CAP + 200


def record(path: Path, sent_message: "Message | Any", text: str) -> None:
    """Append one entry for a voice message the bridge just sent.

    The file comes off `sent_message.voice`, falling back to `.audio` /
    `.document` in case Telegram classified the upload differently; no-ops
    if none of those carry a file_unique_id. Never raises — a failure here
    must not break the send path.
    """
    try:
        media = (
            getattr(sent_message, "voice", None)
            or getattr(sent_message, "audio", None)
            or getattr(sent_message, "document", None)
        )
        file_unique_id = getattr(media, "file_unique_id", None) if media is not None else None
        if not file_unique_id:
            return
        sent_date = getattr(sent_message, "date", None)
        entry = {
            "date": int(sent_date.timestamp()) if sent_date is not None else None,
            "chat_id": getattr(sent_message, "chat_id", None),
            "message_id": getattr(sent_message, "message_id", None),
            "file_unique_id": file_unique_id,
            "text": text,
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim_if_needed(target)
    except Exception as exc:  # noqa: BLE001 — must never break the send path
        logger.warning("voice_ledger.record failed: %s", exc)


def _trim_if_needed(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= _TRIM_AT:
        return
    keep = lines[-_CAP:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def lookup(
    path: Path, *, file_unique_id: str | None = None, date: int | None = None
) -> dict | None:
    """Return the newest ledger entry matching `file_unique_id`; if that
    misses (or none was given) and `date` is given, fall back to an exact
    match on `date`. Missing file, corrupt lines, or no match -> None;
    never raises."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    if file_unique_id is not None:
        for entry in reversed(entries):
            if entry.get("file_unique_id") == file_unique_id:
                return entry
    if date is not None:
        for entry in reversed(entries):
            if entry.get("date") == date:
                return entry
    return None
