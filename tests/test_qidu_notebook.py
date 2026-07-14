"""Tests for NotebookSync."""

from __future__ import annotations

import os
from unittest.mock import patch

from synapse_core.qidu_notebook import NotebookSync, safe_title


def make_sync(tmp_path, **kwargs) -> NotebookSync:
    defaults = dict(
        api_base="http://example.com/api",
        token="tok",
        notebook_dir=tmp_path / "vault",
        sync_every=1,
        lock_path=str(tmp_path / "notebook.lock"),
    )
    defaults.update(kwargs)
    return NotebookSync(**defaults)


def book_md(book_id: str, title: str) -> str:
    return f"---\ntitle: {title}\nbook_id: {book_id}\n---\n\n> hi\n"


# ── disabled ─────────────────────────────────────────────────────────────

def test_disabled_without_notebook_dir(tmp_path):
    sync = make_sync(tmp_path, notebook_dir=None)
    assert sync.enabled is False
    with patch.object(sync, "_fetch_dirty") as mock_fetch:
        sync.tick()
    mock_fetch.assert_not_called()


# ── dirty → pull → write chain ──────────────────────────────────────────

def test_full_sync_writes_file(tmp_path):
    sync = make_sync(tmp_path)
    books = [{"book_id": "bk-1", "title": "我的书"}]
    md = book_md("bk-1", "我的书")

    with patch.object(sync, "_fetch_dirty", return_value=books) as mock_dirty, \
         patch.object(sync, "_fetch_export", return_value=md) as mock_export:
        sync.tick()

    mock_dirty.assert_called_once_with(all_pass=True)
    mock_export.assert_called_once_with("bk-1")
    target = tmp_path / "vault" / "我的书.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == md


def test_first_pass_uses_all_flag_then_dirty_only(tmp_path):
    sync = make_sync(tmp_path)
    with patch.object(sync, "_fetch_dirty", return_value=[]) as mock_dirty:
        sync.tick()
        sync.tick()

    calls = mock_dirty.call_args_list
    assert calls[0].kwargs == {"all_pass": True}
    assert calls[1].kwargs == {"all_pass": False}


# ── filename sanitize ────────────────────────────────────────────────────

def test_safe_title_replaces_unsafe_chars():
    assert safe_title('a/b\\c:d*e?f"g<h>i|j') == "a·b·c·d·e·f·g·h·i·j"


def test_safe_title_empty_falls_back():
    assert safe_title("") == "untitled"


# ── collision → suffix routing ───────────────────────────────────────────

def test_collision_different_book_gets_suffix(tmp_path):
    sync = make_sync(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    existing = vault / "同名.md"
    existing.write_text(book_md("bk-old", "同名"), encoding="utf-8")

    books = [{"book_id": "bk-new", "title": "同名"}]
    md = book_md("bk-new", "同名")

    with patch.object(sync, "_fetch_dirty", return_value=books), \
         patch.object(sync, "_fetch_export", return_value=md):
        sync.tick()

    # original untouched, new book routed to suffixed filename
    assert existing.read_text(encoding="utf-8") == book_md("bk-old", "同名")
    suffixed = vault / "同名-bk-new.md"
    assert suffixed.exists()
    assert suffixed.read_text(encoding="utf-8") == md


def test_same_book_reuses_same_file_no_suffix(tmp_path):
    sync = make_sync(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    existing = vault / "同名.md"
    existing.write_text(book_md("bk-1", "同名"), encoding="utf-8")

    books = [{"book_id": "bk-1", "title": "同名"}]
    updated_md = book_md("bk-1", "同名") + "更新内容\n"

    with patch.object(sync, "_fetch_dirty", return_value=books), \
         patch.object(sync, "_fetch_export", return_value=updated_md):
        sync.tick()

    assert existing.read_text(encoding="utf-8") == updated_md
    assert not (vault / "同名-bk-1.md").exists()


# ── flock ────────────────────────────────────────────────────────────────

def test_flock_held_elsewhere_skips_tick(tmp_path):
    import fcntl

    lock_path = str(tmp_path / "notebook.lock")
    sync = make_sync(tmp_path, lock_path=lock_path)

    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with patch.object(sync, "_fetch_dirty") as mock_dirty:
            sync.tick()
        mock_dirty.assert_not_called()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_flock_released_allows_next_tick(tmp_path):
    sync = make_sync(tmp_path)
    with patch.object(sync, "_fetch_dirty", return_value=[]) as mock_dirty:
        sync.tick()
    mock_dirty.assert_called_once()


# ── sync_every gating ────────────────────────────────────────────────────

def test_only_syncs_every_nth_tick(tmp_path):
    sync = make_sync(tmp_path, sync_every=12)
    with patch.object(sync, "_fetch_dirty", return_value=[]) as mock_dirty:
        for _ in range(11):
            sync.tick()
        mock_dirty.assert_not_called()
        sync.tick()
        mock_dirty.assert_called_once()


# ── network failure ──────────────────────────────────────────────────────

def test_dirty_fetch_failure_skips_pass_silently(tmp_path):
    sync = make_sync(tmp_path)
    with patch.object(sync, "_fetch_dirty", side_effect=OSError("boom")):
        sync.tick()  # must not raise
    assert sync._first_pass_done is False


def test_export_fetch_failure_skips_that_book_only(tmp_path):
    sync = make_sync(tmp_path)
    books = [{"book_id": "bk-bad", "title": "坏书"}, {"book_id": "bk-good", "title": "好书"}]
    good_md = book_md("bk-good", "好书")

    def fake_export(bid):
        if bid == "bk-bad":
            raise OSError("network down")
        return good_md

    with patch.object(sync, "_fetch_dirty", return_value=books), \
         patch.object(sync, "_fetch_export", side_effect=fake_export):
        sync.tick()

    assert not (tmp_path / "vault" / "坏书.md").exists()
    assert (tmp_path / "vault" / "好书.md").read_text(encoding="utf-8") == good_md


# ── atomic write ─────────────────────────────────────────────────────────

def test_atomic_write_no_partial_file_on_failure(tmp_path):
    sync = make_sync(tmp_path)
    books = [{"book_id": "bk-1", "title": "书"}]
    md = book_md("bk-1", "书")

    with patch.object(sync, "_fetch_dirty", return_value=books), \
         patch.object(sync, "_fetch_export", return_value=md), \
         patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        sync.tick()

    target = tmp_path / "vault" / "书.md"
    tmp_file = tmp_path / "vault" / "书.md.tmp"
    assert not target.exists()
    assert not tmp_file.exists()


def test_atomic_write_replaces_existing_file_cleanly(tmp_path):
    sync = make_sync(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "书.md"
    target.write_text("old content", encoding="utf-8")

    books = [{"book_id": "bk-1", "title": "书"}]
    md = book_md("bk-1", "书")

    with patch.object(sync, "_fetch_dirty", return_value=books), \
         patch.object(sync, "_fetch_export", return_value=md):
        sync.tick()

    assert target.read_text(encoding="utf-8") == md
    assert not (vault / "书.md.tmp").exists()


# ── mkdir before write ───────────────────────────────────────────────────

def test_notebook_dir_created_if_missing(tmp_path):
    sync = make_sync(tmp_path)
    assert not (tmp_path / "vault").exists()
    books = [{"book_id": "bk-1", "title": "书"}]
    md = book_md("bk-1", "书")

    with patch.object(sync, "_fetch_dirty", return_value=books), \
         patch.object(sync, "_fetch_export", return_value=md):
        sync.tick()

    assert (tmp_path / "vault").is_dir()
    assert (tmp_path / "vault" / "书.md").exists()
