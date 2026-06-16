"""Cursor persistence tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from synapse_wx.ilink.cursor import Cursor


@pytest.fixture
def cursor_path(tmp_path: Path) -> Path:
    return tmp_path / "cursor.json"


def test_get_empty_returns_empty_string(cursor_path: Path) -> None:
    c = Cursor(cursor_path)
    assert c.get() == ""


def test_set_then_get_roundtrips(cursor_path: Path) -> None:
    c = Cursor(cursor_path)
    c.set("abc123")
    assert c.get() == "abc123"
    # Sibling read sees the same value
    assert Cursor(cursor_path).get() == "abc123"


def test_set_strips_trailing_whitespace(cursor_path: Path) -> None:
    c = Cursor(cursor_path)
    c.set("token-xyz\n")
    assert c.get() == "token-xyz"


def test_atomic_write_no_stale_tmp(cursor_path: Path) -> None:
    """After a successful write, no .tmp residue should remain."""
    c = Cursor(cursor_path)
    c.set("v1")
    c.set("v2")
    siblings = list(cursor_path.parent.iterdir())
    tmps = [p for p in siblings if p.name.endswith(".tmp")]
    assert tmps == []
    assert c.get() == "v2"


def test_atomic_write_cleans_tmp_on_failure(
    cursor_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails mid-write, the .tmp file is removed."""
    c = Cursor(cursor_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        c.set("doomed")

    tmps = [p for p in cursor_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert tmps == []


def test_chmod_600_after_write(cursor_path: Path) -> None:
    c = Cursor(cursor_path)
    c.set("secret")
    mode = stat.S_IMODE(cursor_path.stat().st_mode)
    assert mode == 0o600


def test_clear_removes_file(cursor_path: Path) -> None:
    c = Cursor(cursor_path)
    c.set("v")
    assert cursor_path.exists()
    c.clear()
    assert not cursor_path.exists()
    assert c.get() == ""
