"""Tests for QiduSignalPoller."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.qidu_signal import QiduSignalPoller, render_signal, _MAX_CONSECUTIVE_FAILURES


def make_poller(channel: str, last_active_path, alerts=None, user_name: str = "测试用户") -> QiduSignalPoller:
    return QiduSignalPoller(
        api_base="http://example.com/api",
        token="tok",
        channel=channel,
        user_name=user_name,
        last_active_path=last_active_path,
        alerts=alerts,
    )


def write_last_active(path, channel: str, ts: float = 1000.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sid": "s1", "channel": channel, "ts": ts}))


# ── should_poll routing matrix ──────────────────────────────────────────────

def test_active_tg_only_tg_polls(tmp_path):
    la = tmp_path / "last_active.json"
    write_last_active(la, "tg")
    assert make_poller("tg", la).should_poll() is True
    assert make_poller("wx", la).should_poll() is False


def test_active_wx_only_wx_polls(tmp_path):
    la = tmp_path / "last_active.json"
    write_last_active(la, "wx")
    assert make_poller("wx", la).should_poll() is True
    assert make_poller("tg", la).should_poll() is False


def test_active_cli_only_tg_polls(tmp_path):
    la = tmp_path / "last_active.json"
    write_last_active(la, "cli")
    assert make_poller("tg", la).should_poll() is True
    assert make_poller("wx", la).should_poll() is False


def test_missing_file_only_tg_polls(tmp_path):
    la = tmp_path / "does_not_exist.json"
    assert make_poller("tg", la).should_poll() is True
    assert make_poller("wx", la).should_poll() is False


# ── render_signal ────────────────────────────────────────────────────────────

def test_render_highlight_contains_ids():
    text = render_signal("highlight", {
        "book_id": "bk-1", "book_title": "书名", "chapter_id": "ch-1",
        "chapter_title": "第一章", "paragraph_id": "p-1", "highlight_id": 42,
        "quoted_text": "原文片段",
    }, "测试用户")
    assert "bk-1" in text
    assert "42" in text
    assert "原文片段" in text
    assert "book_annotate" in text
    assert "测试用户" in text


def test_render_annotation_contains_annotation_text():
    text = render_signal("annotation", {
        "book_id": "bk-1", "book_title": "书名", "chapter_id": "ch-1",
        "chapter_title": "第一章", "paragraph_id": "p-1", "highlight_id": 42,
        "quoted_text": "原文", "annotation_id": 7, "annotation_text": "批注内容",
        "parent_id": None,
    }, "测试用户")
    assert "批注内容" in text
    assert "annotation_id=7" in text
    assert "测试用户" in text


def test_render_reply_mentions_parent_id():
    text = render_signal("reply", {
        "book_id": "bk-1", "book_title": "书名", "chapter_id": "ch-1",
        "chapter_title": "第一章", "paragraph_id": "p-1", "highlight_id": 42,
        "quoted_text": "原文", "annotation_id": 9, "annotation_text": "回复内容",
        "parent_id": 5,
    }, "测试用户")
    assert "回复内容" in text
    assert "parent_id=9" in text
    assert "测试用户" in text


def test_render_default_user_name_falls_back_to_generic():
    text = render_signal("highlight", {
        "book_id": "bk-1", "book_title": "书名", "chapter_id": "ch-1",
        "chapter_title": "第一章", "paragraph_id": "p-1", "highlight_id": 42,
        "quoted_text": "原文片段",
    })
    assert "用户" in text


def test_render_unknown_event_type_returns_none():
    assert render_signal("unknown", {}) is None


def test_render_missing_field_returns_none():
    assert render_signal("highlight", {"book_id": "bk-1"}) is None


# ── fetch ────────────────────────────────────────────────────────────────────

def test_fetch_renders_pending_signals(tmp_path):
    la = tmp_path / "last_active.json"
    poller = make_poller("tg", la)
    payload = {
        "signals": [
            {
                "id": 1, "event_type": "highlight",
                "payload": {
                    "book_id": "bk-1", "book_title": "书名", "chapter_id": "ch-1",
                    "chapter_title": "第一章", "paragraph_id": "p-1",
                    "highlight_id": 42, "quoted_text": "原文",
                },
            },
        ],
    }
    with patch.object(poller, "_http", return_value=payload):
        texts = poller.fetch()
    assert len(texts) == 1
    assert "bk-1" in texts[0]
    assert "highlight_id=42" in texts[0]
    assert "测试用户" in texts[0]


def test_fetch_network_error_returns_empty_silently(tmp_path):
    la = tmp_path / "last_active.json"
    poller = make_poller("tg", la)
    with patch.object(poller, "_http", side_effect=OSError("connection refused")):
        texts = poller.fetch()
    assert texts == []


def test_fetch_ten_consecutive_failures_fires_one_alert(tmp_path):
    la = tmp_path / "last_active.json"
    alerts = MagicMock()
    poller = make_poller("tg", la, alerts=alerts)
    with patch.object(poller, "_http", side_effect=OSError("down")):
        for _ in range(_MAX_CONSECUTIVE_FAILURES - 1):
            poller.fetch()
        alerts.write.assert_not_called()
        poller.fetch()
    alerts.write.assert_called_once()
    assert poller._fail_count == 0


def test_fetch_success_resets_fail_count(tmp_path):
    la = tmp_path / "last_active.json"
    poller = make_poller("tg", la)
    with patch.object(poller, "_http", side_effect=OSError("down")):
        poller.fetch()
    assert poller._fail_count == 1
    with patch.object(poller, "_http", return_value={"signals": []}):
        poller.fetch()
    assert poller._fail_count == 0
