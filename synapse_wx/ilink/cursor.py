"""Cursor persistence — atomic write so retry/restart resumes cleanly."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".config" / "synapse-wx" / "cursor.json"


class Cursor:
    """Tiny wrapper around a single-string cursor file with atomic writes."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = Path(path)

    def get(self) -> str:
        try:
            if self.path.exists():
                return self.path.read_text().strip()
        except OSError as e:
            logger.warning("Failed to load cursor: %s", e)
        return ""

    def set(self, value: str) -> None:
        """Atomic write: tmp + os.replace, then chmod 600 on the final file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(value)
            os.replace(tmp, self.path)
        except OSError:
            # Clean stale tmp so a crashed write doesn't leave residue
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError as e:
                logger.warning("Failed to clear cursor: %s", e)
