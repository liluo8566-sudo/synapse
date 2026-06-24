"""Tests for QiduParser."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from synapse_core.qidu_parser import QiduParser, _LOCK_PATH, _MAX_FAILURES, _RAPID_FAIL_THRESHOLD


# ── helpers ────────────────────────────────────────────────────────────────────

def make_parser(**kwargs) -> QiduParser:
    defaults = dict(
        api_base="http://example.com/api",
        token="tok",
        binary="claude",
        max_concurrent=2,
        poll_interval=0.05,
        extract_script="/tmp/extract.py",
    )
    defaults.update(kwargs)
    return QiduParser(**defaults)


def fake_book(book_id="b-001", filename="book.epub", fmt="epub") -> dict:
    return {"book_id": book_id, "filename": filename, "format": fmt}


def stream_lines(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


class FakeProc:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, stdout_text="", exit_code=0):
        self.stdin = MagicMock()
        self.stdout = StringIO(stdout_text)
        self.stderr = StringIO("")
        self._exit_code = exit_code
        self._killed = False
        self._pid = 99999

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        return self._exit_code

    def kill(self):
        self._killed = True


# ── happy path ─────────────────────────────────────────────────────────────────

def test_happy_path_single_book():
    """result event → parse-status=1 → success, fail_count cleared."""
    parser = make_parser()
    book = fake_book()
    bid = book["book_id"]

    result_stdout = stream_lines(
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    )
    proc = FakeProc(stdout_text=result_stdout, exit_code=0)

    with patch.object(parser, "_check_parse_status", return_value=1) as mock_status, \
         patch.object(parser, "_report_failed") as mock_fail, \
         patch("subprocess.Popen", return_value=proc):

        parser._parse_one(book)

    mock_status.assert_called_once_with(bid)
    mock_fail.assert_not_called()
    assert parser._fail_count.get(bid, 0) == 0


def test_happy_path_parse_status_confirmed():
    """parse-status=1 means success; fail_count should not increment."""
    parser = make_parser()
    book = fake_book("b-002")
    bid = book["book_id"]

    stdout = stream_lines({"type": "result"})
    proc = FakeProc(stdout_text=stdout)

    with patch("subprocess.Popen", return_value=proc), \
         patch.object(parser, "_check_parse_status", return_value=1), \
         patch.object(parser, "_report_failed") as mock_fail:
        parser._parse_one(book)

    mock_fail.assert_not_called()
    assert parser._fail_count.get(bid, 0) == 0


# ── concurrency ────────────────────────────────────────────────────────────────

def test_concurrency_max_two_of_three():
    """3 pending books, max_concurrent=2 → 2 start, 1 skipped this round."""
    parser = make_parser(max_concurrent=2)
    books = [fake_book(f"b-{i:03d}") for i in range(3)]

    spawned = []

    def fake_spawn(book):
        bid = book["book_id"]
        class _Stub:
            def poll(self): return None
        with parser._lock:
            parser._active[bid] = _Stub()
        spawned.append(bid)

    with patch.object(parser, "_fetch_pending", return_value=books), \
         patch.object(parser, "_spawn_parser", side_effect=fake_spawn):
        parser._poll_once()

    assert len(spawned) == 2


def test_concurrency_already_active_skipped():
    """Book already in _active must not be spawned again."""
    parser = make_parser(max_concurrent=2)
    book = fake_book("b-already")

    class _Stub:
        def poll(self): return None

    with parser._lock:
        parser._active["b-already"] = _Stub()

    with patch.object(parser, "_fetch_pending", return_value=[book]), \
         patch.object(parser, "_spawn_parser") as mock_spawn:
        parser._poll_once()

    mock_spawn.assert_not_called()


# ── failure retry ──────────────────────────────────────────────────────────────

def test_three_consecutive_failures_calls_report_failed():
    """3 non-rapid failures → _report_failed called, fail_count cleared."""
    parser = make_parser()
    book = fake_book("b-fail")
    bid = book["book_id"]

    # No result event in stdout → proc exits unexpectedly → rapid=True
    # To test non-rapid path, use got_result=True but parse-status != 1
    stdout = stream_lines({"type": "result"})
    proc = FakeProc(stdout_text=stdout, exit_code=0)

    with patch("subprocess.Popen", return_value=proc), \
         patch.object(parser, "_check_parse_status", return_value=0), \
         patch.object(parser, "_report_failed") as mock_fail:

        # Three failures — each _parse_one call increments
        parser._parse_one(book)
        proc.stdout = StringIO(stdout)  # reset
        parser._parse_one(book)
        proc.stdout = StringIO(stdout)
        parser._parse_one(book)

    mock_fail.assert_called_once_with(bid)
    assert bid not in parser._fail_count


def test_failure_count_increments_each_round():
    parser = make_parser()
    book = fake_book("b-count")
    bid = book["book_id"]

    stdout = stream_lines({"type": "result"})
    proc = FakeProc(stdout_text=stdout)

    with patch("subprocess.Popen", return_value=proc), \
         patch.object(parser, "_check_parse_status", return_value=0), \
         patch.object(parser, "_report_failed"):

        parser._parse_one(book)
        assert parser._fail_count.get(bid, 0) == 1
        proc.stdout = StringIO(stdout)
        parser._parse_one(book)
        assert parser._fail_count.get(bid, 0) == 2


# ── quota mode ─────────────────────────────────────────────────────────────────

def test_enter_quota_mode_sets_flag():
    parser = make_parser()
    with patch.object(parser, "_report_quota") as mock_rq:
        parser._enter_quota_mode("b-quota")
    assert parser._quota_exhausted is True
    mock_rq.assert_called_once_with("b-quota")


def test_probe_quota_success_clears_flag():
    parser = make_parser()
    parser._quota_exhausted = True
    parser._last_quota_probe = 0  # force probe

    with patch.object(parser, "_probe_quota", return_value=True), \
         patch.object(parser, "_retry_quota") as mock_rr:
        parser._maybe_probe_quota()

    assert parser._quota_exhausted is False
    mock_rr.assert_called_once()


def test_probe_quota_failure_keeps_flag():
    parser = make_parser()
    parser._quota_exhausted = True
    parser._last_quota_probe = 0

    with patch.object(parser, "_probe_quota", return_value=False), \
         patch.object(parser, "_retry_quota") as mock_rr:
        parser._maybe_probe_quota()

    assert parser._quota_exhausted is True
    mock_rr.assert_not_called()


def test_probe_quota_respects_interval():
    """_maybe_probe_quota should not probe again within interval."""
    parser = make_parser()
    parser._quota_exhausted = True
    parser._last_quota_probe = time.time()  # just probed

    with patch.object(parser, "_probe_quota") as mock_probe:
        parser._maybe_probe_quota()

    mock_probe.assert_not_called()


def test_rapid_failures_enter_quota_mode():
    """N rapid non-timeout exits within window → quota mode."""
    parser = make_parser()
    book = fake_book("b-rapid")

    # Simulate rapid failures by directly calling _record_failure(rapid=True)
    with patch.object(parser, "_enter_quota_mode") as mock_enter, \
         patch.object(parser, "_report_failed"):
        for i in range(_RAPID_FAIL_THRESHOLD - 1):
            parser._record_failure(book["book_id"], rapid=True)
        mock_enter.assert_not_called()
        parser._record_failure(book["book_id"], rapid=True)
        mock_enter.assert_called_once()


# ── flock mutual exclusion ─────────────────────────────────────────────────────

def test_flock_only_one_polls():
    """Two parsers: first acquires lock, second cannot."""
    p1 = make_parser()
    p2 = make_parser()

    assert p1._try_acquire_lock() is True
    try:
        result = p2._try_acquire_lock()
        assert result is False
    finally:
        p1._release_lock()
        # clean up lock file
        try:
            os.remove(_LOCK_PATH)
        except FileNotFoundError:
            pass


def test_flock_released_allows_second():
    """After first releases, second can acquire."""
    p1 = make_parser()
    p2 = make_parser()

    assert p1._try_acquire_lock() is True
    p1._release_lock()

    try:
        assert p2._try_acquire_lock() is True
    finally:
        p2._release_lock()
        try:
            os.remove(_LOCK_PATH)
        except FileNotFoundError:
            pass


# ── idempotent (already active) ────────────────────────────────────────────────

def test_poll_once_skips_active_books():
    """pending returns a book already in _active → spawn not called."""
    parser = make_parser(max_concurrent=2)
    book = fake_book("b-skip")
    bid = book["book_id"]

    class _Stub:
        def poll(self): return None

    with parser._lock:
        parser._active[bid] = _Stub()

    with patch.object(parser, "_fetch_pending", return_value=[book]), \
         patch.object(parser, "_spawn_parser") as mock_spawn:
        parser._poll_once()

    mock_spawn.assert_not_called()


# ── build_prompt ───────────────────────────────────────────────────────────────

def test_build_prompt_contains_key_fields():
    parser = make_parser(
        api_base="http://host/api",
        token="mytoken",
        extract_script="/opt/extract.py",
    )
    book = fake_book("bk-123", "novel.epub", "epub")
    prompt = parser._build_prompt(book)

    assert "mytoken" in prompt
    assert "http://host/api" in prompt
    assert "bk-123" in prompt
    assert "novel.epub" in prompt
    assert "epub" in prompt
    assert "/opt/extract.py" in prompt
    assert "parse-result" in prompt
    assert "第一步" in prompt
    assert "第六步" in prompt


# ── cleanup ────────────────────────────────────────────────────────────────────

def test_cleanup_tmp_removes_files(tmp_path):
    parser = make_parser()
    book = fake_book("bk-clean", "clean.epub")

    book_dir = tmp_path / "bk-clean"
    book_dir.mkdir()
    (book_dir / "text.txt").write_text("content")
    file_path = tmp_path / "clean.epub"
    file_path.write_text("data")

    with patch("synapse_core.qidu_parser._TMP_BASE", str(tmp_path)):
        parser._cleanup_tmp(book)

    assert not book_dir.exists()
    assert not file_path.exists()


# ── reap_finished ──────────────────────────────────────────────────────────────

def test_reap_finished_removes_exited_procs():
    parser = make_parser()
    alive = MagicMock()
    alive.poll.return_value = None
    dead = MagicMock()
    dead.poll.return_value = 0

    with parser._lock:
        parser._active["alive"] = alive
        parser._active["dead"] = dead

    parser._reap_finished()

    with parser._lock:
        assert "alive" in parser._active
        assert "dead" not in parser._active
