"""B6 — last_active.json atomic read/write.

`~/.config/marrow/last_active.json` is the cross-channel pointer the bridge +
cli both touch every turn so `/resume` knows which session is freshest. The
schema is intentionally tiny: `{sid, channel, ts}`. Lock-free atomic rename.
Callers pass the destination path explicitly so tests + sandboxes don't
clobber a real file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from synapse_core import last_active


@pytest.fixture(autouse=True)
def _target(tmp_path: Path) -> Path:
    return tmp_path / "last_active.json"


def test_read_missing_file_returns_none(_target: Path) -> None:
    assert last_active.read(_target) is None


def test_write_then_read_roundtrip(_target: Path) -> None:
    last_active.write(_target, "sid-abc", channel="wx", ts=1733000000.0)
    got = last_active.read(_target)
    assert got == {"sid": "sid-abc", "channel": "wx", "ts": 1733000000.0}


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "last_active.json"
    last_active.write(nested, "sid-x", channel="wx", ts=1.0)
    assert nested.is_file()
    payload = json.loads(nested.read_text())
    assert payload == {"sid": "sid-x", "channel": "wx", "ts": 1.0}


def test_write_is_atomic_via_rename(_target: Path) -> None:
    # Pre-populate with stale content; a partial write must never expose it.
    _target.parent.mkdir(parents=True, exist_ok=True)
    _target.write_text('{"sid":"old","channel":"cli","ts":0.0}')
    last_active.write(_target, "sid-new", channel="wx", ts=42.0)
    # No leftover tmp file in the parent dir.
    leftovers = [p.name for p in _target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    got = last_active.read(_target)
    assert got["sid"] == "sid-new"
    assert got["channel"] == "wx"
    assert got["ts"] == 42.0


def test_write_overwrites_existing(_target: Path) -> None:
    last_active.write(_target, "first", channel="wx", ts=1.0)
    last_active.write(_target, "second", channel="cli", ts=2.0)
    got = last_active.read(_target)
    assert got == {"sid": "second", "channel": "cli", "ts": 2.0}


def test_read_malformed_json_returns_none(_target: Path) -> None:
    _target.parent.mkdir(parents=True, exist_ok=True)
    _target.write_text("not json{")
    assert last_active.read(_target) is None


def test_read_non_dict_returns_none(_target: Path) -> None:
    _target.parent.mkdir(parents=True, exist_ok=True)
    _target.write_text('["not", "a", "dict"]')
    assert last_active.read(_target) is None


def test_write_empty_sid_is_noop(_target: Path) -> None:
    # Defensive: never write a placeholder row with no sid (loop calls this
    # after every turn so a missing sid is "not initialised yet").
    last_active.write(_target, "", channel="wx", ts=1.0)
    assert not _target.exists()
    assert last_active.read(_target) is None


def test_write_failure_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic rename failure must not leave a stale .tmp behind."""
    target = tmp_path / "last_active.json"

    real_replace = os.replace

    def boom(src: str, dst: str) -> None:
        # Simulate a mid-write OS failure on the rename step.
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    # write() must swallow the error (best-effort) and clean the tmp.
    last_active.write(target, "sid-z", channel="wx", ts=1.0)
    monkeypatch.setattr(os, "replace", real_replace)
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
