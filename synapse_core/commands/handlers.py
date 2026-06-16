"""Thin wrappers / TODO stubs for command handlers wired in later phases.

B7 owns the replay read + format primitive only. The slash-dispatch
itself lives in `synapse_core/commands/registry.py` — B6 will wire the
`/resume <sid>` path to call `replay_for_channel()` then push the bubbles
via the loop's outbound channel. See synapse-wx/docs/notes/reference.md.
"""

from __future__ import annotations

from .. import replay


def replay_for_channel(
    sid: str,
    *,
    n: int = 2,
    cwd: str | None = None,
) -> list[str]:
    """Read last n turns of sid and return channel-ready bubbles.

    Empty list when the jsonl is missing or has no qualifying turns.
    Callers (B6 dispatch) should iterate the result and emit each entry
    as a separate `ILinkClient.send_text`.
    """
    turns = replay.read_last_n_turns(sid, n=n, cwd=cwd)
    return replay.format_for_channel(turns)


# TODO(B6): register `/resume <sid>` to call replay_for_channel(sid) and
# fan each bubble out via the loop's outbound sender BEFORE swapping the
# provider so the user sees the replay arrive ahead of the new prompt.
