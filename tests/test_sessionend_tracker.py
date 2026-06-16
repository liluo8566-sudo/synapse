"""Tests for SessionTracker — atomic persistence + chmod 600 + round-trip."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from synapse_core.sessionend.tracker import SessionTracker


@pytest.fixture()
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.json"


def test_set_get_round_trip(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    t.set("user-1", "sid-abc")
    assert t.get("user-1") == "sid-abc"
    assert t.get("missing") is None


def test_set_persists_to_disk(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    t.set("user-1", "sid-abc")
    t.set("user-2", "sid-xyz")

    data = json.loads(state_path.read_text())
    assert data == {"user-1": "sid-abc", "user-2": "sid-xyz"}


def test_reload_from_disk(state_path: Path) -> None:
    t1 = SessionTracker(state_path=state_path)
    t1.set("user-1", "sid-abc")
    t1.set("user-2", "sid-xyz")

    t2 = SessionTracker(state_path=state_path)
    assert t2.get("user-1") == "sid-abc"
    assert t2.get("user-2") == "sid-xyz"
    assert t2.snapshot() == {"user-1": "sid-abc", "user-2": "sid-xyz"}


def test_forget_returns_old_and_removes(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    t.set("user-1", "sid-abc")
    t.set("user-2", "sid-xyz")

    old = t.forget("user-1")
    assert old == "sid-abc"
    assert t.get("user-1") is None

    # disk reflects removal
    data = json.loads(state_path.read_text())
    assert data == {"user-2": "sid-xyz"}


def test_forget_missing_returns_none(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    assert t.forget("never-set") is None


def test_chmod_600_on_state_file(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    t.set("user-1", "sid-abc")

    mode = state_path.stat().st_mode & 0o777
    expected = stat.S_IRUSR | stat.S_IWUSR
    assert mode == expected, f"expected 0o600, got {oct(mode)}"


def test_set_empty_raises(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    with pytest.raises(ValueError):
        t.set("", "sid")
    with pytest.raises(ValueError):
        t.set("user", "")


def test_snapshot_is_copy(state_path: Path) -> None:
    t = SessionTracker(state_path=state_path)
    t.set("user-1", "sid-abc")
    snap = t.snapshot()
    snap["mutated"] = "should-not-leak"
    assert "mutated" not in t.snapshot()


def test_load_tolerates_missing_file(tmp_path: Path) -> None:
    # no file present — must not raise
    t = SessionTracker(state_path=tmp_path / "missing.json")
    assert t.snapshot() == {}


def test_load_tolerates_bad_json(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{ not json")
    t = SessionTracker(state_path=state_path)
    assert t.snapshot() == {}
