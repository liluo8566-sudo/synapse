"""B7 cross-client history replay — tail-read cc jsonl, format for WeChat.

PLAN.md L68-L71: on `/resume <sid>`, bridge reads last 2 turns (user×2 +
assistant×2) from `~/.claude/projects/<slug>/<sid>.jsonl`, sends each with
a `[回放]` prefix. Pure read, no writeback, zero token cost.
"""

from __future__ import annotations

import json
from pathlib import Path

from synapse_core import replay

# ── slug derivation ─────────────────────────────────────────────────────────


def test_slug_for_cwd_prefixes_with_dash_and_replaces_slashes() -> None:
    assert replay.slug_for_cwd("/Users/Gabrielle/CC-Lab/marrow") == (
        "-Users-Gabrielle-CC-Lab-marrow"
    )


def test_slug_for_cwd_handles_relative_path_no_leading_dash() -> None:
    # Defensive — bridge always passes absolute, but if not we mirror
    # cc's behaviour: only literal `/`s become `-`, no extra prefix.
    assert replay.slug_for_cwd("home/x") == "home-x"


# ── read_last_n_turns ───────────────────────────────────────────────────────


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False))
            f.write("\n")


def _user(text: str, ts: str = "2026-06-02T10:00:00.000Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str, ts: str = "2026-06-02T10:00:01.000Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert replay.read_last_n_turns("no-such-sid", n=2, cwd=str(tmp_path)) == []


def test_returns_last_n_user_and_assistant_pairs(tmp_path: Path) -> None:
    sid = "abc12345"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events = [
        _user("u1", "2026-06-02T10:00:00.000Z"),
        _assistant("a1", "2026-06-02T10:00:01.000Z"),
        _user("u2", "2026-06-02T10:01:00.000Z"),
        _assistant("a2", "2026-06-02T10:01:01.000Z"),
        _user("u3", "2026-06-02T10:02:00.000Z"),
        _assistant("a3", "2026-06-02T10:02:01.000Z"),
    ]
    _write_jsonl(jsonl, events)

    turns = replay.read_last_n_turns(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    roles = [t["role"] for t in turns]
    texts = [t["content"] for t in turns]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2
    # Chronological order preserved, only the last 2 of each kept.
    assert texts == ["u2", "a2", "u3", "a3"]
    for t in turns:
        assert isinstance(t["ts"], float)


def test_skips_tool_use_thinking_system_summary_and_empty_text(tmp_path: Path) -> None:
    sid = "filterme"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    events: list[dict] = [
        {"type": "summary", "summary": "ignored"},
        {"type": "system", "subtype": "init", "model": "claude-opus-4-6"},
        _user("real-user-1", "2026-06-02T09:00:00.000Z"),
        # tool_result wrapped as a user event — must be skipped.
        {
            "type": "user",
            "timestamp": "2026-06-02T09:00:30.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "ok"}],
            },
        },
        # assistant turn with only thinking + tool_use — no text → skip.
        {
            "type": "assistant",
            "timestamp": "2026-06-02T09:00:45.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "tool_use", "name": "Read"},
                ],
            },
        },
        _assistant("real-asst-1", "2026-06-02T09:01:00.000Z"),
        _user("real-user-2", "2026-06-02T09:02:00.000Z"),
        _assistant("real-asst-2", "2026-06-02T09:03:00.000Z"),
    ]
    _write_jsonl(jsonl, events)

    turns = replay.read_last_n_turns(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    assert [t["content"] for t in turns] == [
        "real-user-1",
        "real-asst-1",
        "real-user-2",
        "real-asst-2",
    ]


def test_handles_corrupt_lines_without_raising(tmp_path: Path) -> None:
    sid = "broken"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write(json.dumps(_user("hello", "2026-06-02T10:00:00.000Z")) + "\n")
        f.write("\n")
        f.write(json.dumps(_assistant("world", "2026-06-02T10:00:01.000Z")) + "\n")

    turns = replay.read_last_n_turns(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    assert [t["content"] for t in turns] == ["hello", "world"]


def test_n_one_returns_one_user_one_assistant(tmp_path: Path) -> None:
    sid = "single"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            _user("u1"),
            _assistant("a1"),
            _user("u2"),
            _assistant("a2"),
        ],
    )
    turns = replay.read_last_n_turns(
        sid, n=1, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    assert [t["content"] for t in turns] == ["u2", "a2"]


def test_concatenates_multi_block_assistant_text(tmp_path: Path) -> None:
    sid = "multiblock"
    slug = replay.slug_for_cwd(str(tmp_path))
    jsonl = tmp_path / ".claude" / "projects" / slug / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            _user("hi"),
            {
                "type": "assistant",
                "timestamp": "2026-06-02T10:00:01.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "part1"},
                        {"type": "tool_use", "name": "Bash"},
                        {"type": "text", "text": "part2"},
                    ],
                },
            },
        ],
    )
    turns = replay.read_last_n_turns(
        sid, n=2, cwd=str(tmp_path), projects_root=tmp_path / ".claude" / "projects"
    )
    assert turns[-1]["role"] == "assistant"
    assert turns[-1]["content"] == "part1\npart2"


# ── format_for_channel ───────────────────────────────────────────────────────


def test_format_for_channel_prefixes_each_bubble_and_keeps_order() -> None:
    turns = [
        {"role": "user", "content": "hello", "ts": 1.0},
        {"role": "assistant", "content": "hi there", "ts": 2.0},
    ]
    bubbles = replay.format_for_channel(turns)
    assert bubbles == [
        "[回放] user: hello",
        "[回放] assistant: hi there",
    ]


def test_format_for_channel_empty_input_returns_empty_list() -> None:
    assert replay.format_for_channel([]) == []
