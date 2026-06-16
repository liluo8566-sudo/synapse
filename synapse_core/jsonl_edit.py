"""B9 — atomic tail-truncate of cc's jsonl session file.

`~/.claude/projects/<slug>/<sid>.jsonl` is the source of truth cc replays
from when invoked with `--resume <sid>`. To implement `/rewind N` and
`/regen` the bridge truncates the file in place (tmp + os.replace) and
respawns cc — cc then re-reads the trimmed history.

Public surface
--------------
`drop_last_n_pairs(sid, n, cwd, projects_root)`
    Remove the last N user/assistant pairs. A "pair" is one user turn
    plus its (possibly missing) assistant reply, walked from the tail.
    Non-chat lines (system / summary / queue-operation / ...) are kept
    verbatim. Returns the dropped events in original file order so the
    caller can flag them via marrow `audit_log session_block=archive`.

`extract_user_text(parsed)`
    Pull the plain text of a real user prompt event so `/regen` can
    resend it through the freshly-spawned cc — cc does not auto-replay
    the trailing unanswered prompt after `--resume`, so the bridge has
    to push it back on stdin itself.

Both helpers tolerate missing files, malformed lines, and N > pairs
without raising. Slug derivation reuses `synapse_core.replay.slug_for_cwd`
so cwd → slug stays in lockstep with the read-side replay logic.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import replay

logger = logging.getLogger(__name__)


def _jsonl_path(
    sid: str, cwd: str | None, projects_root: Path | None
) -> Path | None:
    """Locate the jsonl for sid; mirror `replay._jsonl_path` exactly."""
    root = Path(projects_root) if projects_root else (Path.home() / ".claude" / "projects")
    cwd = cwd if cwd is not None else os.getcwd()
    candidate = root / replay.slug_for_cwd(cwd) / f"{sid}.jsonl"
    if candidate.is_file():
        return candidate
    if not root.is_dir():
        return None
    for slug_dir in root.iterdir():
        if not slug_dir.is_dir():
            continue
        c = slug_dir / f"{sid}.jsonl"
        if c.is_file():
            return c
    return None


def _read_lines(path: Path) -> list[tuple[str, dict | None]]:
    """Return ``(raw_line, parsed_or_None)`` per non-blank line.

    Keeping the raw line preserves bytes for non-chat events we re-emit
    verbatim — re-serialising via ``json.dumps`` could subtly reorder keys
    or rewrite whitespace.
    """
    out: list[tuple[str, dict | None]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.rstrip("\n")
                if not stripped.strip():
                    continue
                try:
                    parsed = json.loads(stripped)
                except (ValueError, UnicodeDecodeError):
                    parsed = None
                if not isinstance(parsed, dict):
                    parsed = None
                out.append((stripped, parsed))
    except OSError as e:
        logger.warning("jsonl_edit read failed for %s: %s", path, e)
        return []
    return out


def _atomic_write(path: Path, lines: list[str]) -> None:
    """Write `lines` to `path` via tmp + os.replace. Best-effort on cleanup."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln)
                if not ln.endswith("\n"):
                    f.write("\n")
    except OSError as e:
        logger.warning("jsonl_edit tmp write failed for %s: %s", tmp, e)
        _silent_unlink(tmp)
        return
    try:
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("jsonl_edit replace failed: %s", e)
        _silent_unlink(tmp)


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def is_real_user_prompt(parsed: dict | None) -> bool:
    """True iff parsed is a `type:user` entry whose content is the user's own
    text (not a tool_result block synthesised by cc's tool loop)."""
    if not isinstance(parsed, dict) or parsed.get("type") != "user":
        return False
    msg = parsed.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def drop_last_n_pairs(
    sid: str,
    n: int,
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> list[dict]:
    """Rewind the last N real user prompts (and everything after them).

    A "real user prompt" is a `type:user` entry whose content is the user's
    own text — tool_result entries (also `type:user`) are skipped. The anchor
    is the Nth real user prompt counted from the tail; every chat line at or
    after that anchor is dropped (the user prompt, its tool_use turns, the
    tool_results, and the final assistant reply). Non-chat lines
    (system / summary / queue) are kept verbatim.

    If fewer than N real prompts exist, drops all of them. Returns the
    dropped chat events in chronological order, or `[]` on missing file,
    n<=0, or no real prompts.
    """
    if not sid or n <= 0:
        return []
    path = _jsonl_path(sid, cwd, projects_root)
    if path is None:
        return []
    entries = _read_lines(path)
    if not entries:
        return []

    real_indices = [
        i for i, (_, parsed) in enumerate(entries) if is_real_user_prompt(parsed)
    ]
    if not real_indices:
        return []

    take = min(n, len(real_indices))
    cut_idx = real_indices[-take]

    dropped: list[dict] = []
    kept_lines: list[str] = []
    for idx, (raw, parsed) in enumerate(entries):
        if idx >= cut_idx and parsed is not None and parsed.get("type") in (
            "user",
            "assistant",
        ):
            dropped.append(parsed)
        else:
            kept_lines.append(raw)

    if not dropped:
        return []

    _atomic_write(path, kept_lines)
    return dropped


def extract_user_text(parsed: dict | None) -> str | None:
    """Return the user-typed text from a `type:user` jsonl event.

    cc stores prompts as either a string (`content: "hi"`) or a list of
    blocks (`content: [{"type":"text","text":"hi"}, ...]`). Tool_result
    user frames are ignored — they are not Lumi's prose. Returns None
    when the event is not a real user prompt or has no text payload.
    """
    if not is_real_user_prompt(parsed):
        return None
    assert isinstance(parsed, dict)
    msg = parsed.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
        return "".join(parts) or None
    return None
