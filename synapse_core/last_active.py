"""B6 — cross-channel last-active session pointer.

The caller-supplied last-active JSON path is touched by every channel after
each successful turn. Schema is intentionally minimal so producers can be
one-line writes:

    {"sid": "...", "channel": "wx" | "cli" | ...,  "ts": <epoch seconds>}

Lock-free: tmp file + `os.replace` is the atomicity contract. Readers
tolerate missing / malformed files by returning `None`.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

def read(path: Path) -> dict | None:
    """Return the parsed `{sid, channel, ts}` dict, or None on miss / malformed."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("last_active read failed: %s", e)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write(path: Path, sid: str, channel: str, ts: float) -> None:
    """Best-effort atomic write. Empty `sid` → no-op so callers can fire
    blindly (e.g. before cc has reported a session id).

    Never raises. A failed `os.replace` cleans up the tmp file so partial
    writes don't accumulate.
    """
    if not sid:
        return
    target = Path(path)
    payload = {"sid": sid, "channel": channel or "", "ts": float(ts)}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("last_active mkdir failed: %s", e)
        return
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        logger.warning("last_active tmp write failed: %s", e)
        _silent_unlink(tmp)
        return
    try:
        os.replace(tmp, target)
    except OSError as e:
        logger.warning("last_active replace failed: %s", e)
        _silent_unlink(tmp)
        return
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
