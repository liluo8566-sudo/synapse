"""Tests for RawPollLogger: active(), _interesting(), log(), and client wiring."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from synapse_wx.ilink import _auth
from synapse_wx.ilink import client as client_module
from synapse_wx.ilink.client import ILinkClient
from synapse_wx.ilink.cursor import Cursor
from synapse_wx.ilink.rawlog import RawPollLogger


# ── helpers ───────────────────────────────────────────────────────────────────


def _dt(date_str: str) -> datetime:
    """Return a timezone-aware datetime at midnight for the given YYYY-MM-DD."""
    d = datetime.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _make_response(status: int = 200, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.content = b""
    resp.text = json.dumps(json_body) if json_body is not None else ""
    resp.json.return_value = json_body if json_body is not None else {}
    resp.raise_for_status.return_value = None
    return resp


# ── active() ─────────────────────────────────────────────────────────────────


def test_active_false_when_until_empty() -> None:
    rpl = RawPollLogger(until="", now=lambda: _dt("2026-06-12"))
    assert rpl.active() is False


def test_active_true_when_today_equals_until() -> None:
    rpl = RawPollLogger(until="2026-06-12", now=lambda: _dt("2026-06-12"))
    assert rpl.active() is True


def test_active_true_when_today_before_until() -> None:
    rpl = RawPollLogger(until="2026-06-20", now=lambda: _dt("2026-06-12"))
    assert rpl.active() is True


def test_active_false_when_today_after_until() -> None:
    rpl = RawPollLogger(until="2026-06-10", now=lambda: _dt("2026-06-12"))
    assert rpl.active() is False


# ── _interesting() ────────────────────────────────────────────────────────────


def test_interesting_true_when_msgs_non_empty() -> None:
    assert RawPollLogger._interesting({"ret": 0, "msgs": [{"x": 1}]}) is True


def test_interesting_false_when_only_boring_keys_and_msgs_empty() -> None:
    data = {"ret": 0, "errmsg": "", "get_updates_buf": "cursor", "msgs": []}
    assert RawPollLogger._interesting(data) is False


def test_interesting_false_when_only_boring_keys_no_msgs() -> None:
    data = {"ret": 0, "errmsg": "ok", "get_updates_buf": "buf"}
    assert RawPollLogger._interesting(data) is False


def test_interesting_true_when_surprise_key_present_with_empty_msgs() -> None:
    """A key like 'typing_event' outside _BORING_KEYS → interesting even if msgs empty."""
    data = {"ret": 0, "msgs": [], "typing_event": {"user": "u1"}}
    assert RawPollLogger._interesting(data) is True


def test_interesting_true_when_surprise_key_no_msgs_key_at_all() -> None:
    data = {"ret": 0, "presence": "online"}
    assert RawPollLogger._interesting(data) is True


# ── log() ─────────────────────────────────────────────────────────────────────


def test_log_writes_jsonl_line_with_ts_and_data(tmp_path: Path) -> None:
    log_path = tmp_path / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-20",
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    data = {"ret": 0, "msgs": [{"type": 1, "text": "hi"}]}
    rpl.log(data)

    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ts" in record
    assert record["data"] == data


def test_log_appends_multiple_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-20",
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    rpl.log({"ret": 0, "msgs": [{"a": 1}]})
    rpl.log({"ret": 0, "msgs": [{"b": 2}]})

    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["data"]["msgs"][0] == {"a": 1}
    assert json.loads(lines[1])["data"]["msgs"][0] == {"b": 2}


def test_log_does_not_write_boring_response(tmp_path: Path) -> None:
    log_path = tmp_path / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-20",
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    rpl.log({"ret": 0, "errmsg": "", "get_updates_buf": "cur", "msgs": []})
    assert not log_path.exists()


def test_log_does_not_write_when_inactive(tmp_path: Path) -> None:
    log_path = tmp_path / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-10",  # past
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    rpl.log({"ret": 0, "msgs": [{"x": 1}]})
    assert not log_path.exists()


def test_log_non_dict_data_is_noop(tmp_path: Path) -> None:
    log_path = tmp_path / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-20",
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    rpl.log("not a dict")  # type: ignore[arg-type]
    rpl.log(None)  # type: ignore[arg-type]
    rpl.log([1, 2, 3])  # type: ignore[arg-type]
    assert not log_path.exists()


def test_log_surprise_key_with_empty_msgs_is_written(tmp_path: Path) -> None:
    log_path = tmp_path / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-20",
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    data = {"ret": 0, "msgs": [], "typing_event": {"user": "u1", "status": 1}}
    rpl.log(data)

    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["data"]["typing_event"]["user"] == "u1"


def test_log_creates_parent_dirs(tmp_path: Path) -> None:
    log_path = tmp_path / "deep" / "nested" / "raw.jsonl"
    rpl = RawPollLogger(
        until="2026-06-20",
        path=log_path,
        now=lambda: _dt("2026-06-12"),
    )
    rpl.log({"ret": 0, "msgs": [{"x": 1}]})
    assert log_path.exists()


# ── client wiring ─────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    token_file = tmp_path / "token.json"
    monkeypatch.setattr(_auth, "TOKEN_FILE", token_file)
    monkeypatch.setattr(client_module, "TOKEN_FILE", token_file)
    return tmp_path


@pytest.fixture
def logged_in_client(isolated_paths: Path, monkeypatch: pytest.MonkeyPatch) -> ILinkClient:
    token_file = isolated_paths / "token.json"
    token_file.write_text(
        json.dumps({"bot_token": "tok-abc", "base_url": "https://ilinkai.weixin.qq.com"})
    )
    cursor = Cursor(isolated_paths / "cursor.json")
    c = ILinkClient(cursor=cursor)
    c._client = MagicMock(spec=httpx.Client)
    return c


def test_poll_messages_logs_before_ret_check(
    logged_in_client: ILinkClient, tmp_path: Path
) -> None:
    """Logger receives the payload even when ret != 0 (logged before ret check)."""
    log_path = tmp_path / "raw.jsonl"
    logged_data: list[dict] = []

    class CapturingLogger:
        def log(self, data: dict) -> None:
            logged_data.append(data)

    logged_in_client._raw_poll_logger = CapturingLogger()
    logged_in_client._client.post.return_value = _make_response(
        200, {"ret": 500, "errmsg": "server sad", "msgs": []}
    )

    result = logged_in_client.poll_messages()

    assert result == []  # ret!=0 returns empty as usual
    assert len(logged_data) == 1
    assert logged_data[0]["ret"] == 500


def test_poll_messages_logs_successful_response(
    logged_in_client: ILinkClient, tmp_path: Path
) -> None:
    """Logger also receives payload for a normal successful poll."""
    logged_data: list[dict] = []

    class CapturingLogger:
        def log(self, data: dict) -> None:
            logged_data.append(data)

    logged_in_client._raw_poll_logger = CapturingLogger()
    payload = {
        "ret": 0,
        "get_updates_buf": "cursor2",
        "msgs": [{"message_type": 1, "from_user_id": "u1", "item_list": []}],
    }
    logged_in_client._client.post.return_value = _make_response(200, payload)

    logged_in_client.poll_messages()

    assert len(logged_data) == 1
    assert logged_data[0]["ret"] == 0


def test_poll_messages_no_logger_no_crash(logged_in_client: ILinkClient) -> None:
    """No logger set → poll_messages runs without AttributeError."""
    assert logged_in_client._raw_poll_logger is None
    logged_in_client._client.post.return_value = _make_response(
        200, {"ret": 0, "get_updates_buf": "", "msgs": []}
    )
    # Should not raise.
    result = logged_in_client.poll_messages()
    assert result == []
