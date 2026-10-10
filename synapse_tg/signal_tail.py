"""Pure file-tail helper: byte offset in, new complete lines + new offset out.

No state of its own — the caller (shell.py) persists the offset. Mirrors
`tail -n 0 -f` semantics: only bytes already on disk at a given offset are
ever read; a trailing partial line (no terminating newline yet) is left
untouched for the next call to complete.
"""

from __future__ import annotations

import os


def tail_new_lines(path: str, offset: int) -> tuple[list[str], int]:
    """New lines appended to `path` since `offset`, and the offset to persist
    next. Missing file -> ([], offset) unchanged. `offset` past the current
    file size (truncated or replaced) -> read from 0. Blank lines are
    dropped; a partial trailing line is not consumed."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    start = offset if 0 <= offset <= size else 0
    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read()
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return [], start  # only a partial line (or nothing) since `start`
    complete = chunk[: last_nl + 1]
    new_offset = start + last_nl + 1
    text = complete.decode("utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines, new_offset
