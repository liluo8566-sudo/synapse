"""Tests for synapse_wx.hooks.channel_marker — channel injection + switch detect."""

from __future__ import annotations

from synapse_wx.hooks.channel_marker import (
    _previous_channel,
    _stamp_last_active,
    build_output,
)


class _FakeReader:
    def __init__(self, data: dict | None) -> None:
        self.data = data

    def read(self) -> dict | None:
        return self.data


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def write(self, sid: str, channel: str, ts: float) -> None:
        self.calls.append((sid, channel, ts))


def test_no_env_defaults_to_cli() -> None:
    out = build_output({}, payload={})
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "[channel: cli]",
        }
    }


def test_empty_env_defaults_to_cli() -> None:
    assert (
        build_output({"MARROW_CHANNEL": ""})["hookSpecificOutput"]["additionalContext"]
        == "[channel: cli]"
    )
    assert (
        build_output({"MARROW_CHANNEL": "   "})["hookSpecificOutput"][
            "additionalContext"
        ]
        == "[channel: cli]"
    )


def test_wx_channel_injects_marker() -> None:
    out = build_output({"MARROW_CHANNEL": "wx"})
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: wx]"


def test_other_channel_passthrough() -> None:
    out = build_output({"MARROW_CHANNEL": "slack"})
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: slack]"


def test_channel_value_stripped() -> None:
    out = build_output({"MARROW_CHANNEL": "  wx  "})
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: wx]"


def test_switch_detected_when_sid_matches_and_channel_differs() -> None:
    reader = _FakeReader({"sid": "abc", "channel": "wx", "ts": 1.0})
    out = build_output(
        {"MARROW_CHANNEL": ""},
        payload={"session_id": "abc"},
        reader=reader,
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[channel: cli <- wx]")
    assert "CROSS-CHANNEL CONTINUATION" in ctx
    assert "same session" in ctx


def test_switch_arrow_wx_then_cli() -> None:
    reader = _FakeReader({"sid": "abc", "channel": "cli", "ts": 1.0})
    out = build_output(
        {"MARROW_CHANNEL": "wx"},
        payload={"session_id": "abc"},
        reader=reader,
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[channel: wx <- cli]")
    assert "CROSS-CHANNEL CONTINUATION" in ctx
    assert "same session" in ctx


def test_same_channel_no_arrow() -> None:
    reader = _FakeReader({"sid": "abc", "channel": "cli", "ts": 1.0})
    out = build_output(
        {"MARROW_CHANNEL": "cli"},
        payload={"session_id": "abc"},
        reader=reader,
    )
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: cli]"


def test_different_sid_ignored() -> None:
    reader = _FakeReader({"sid": "other", "channel": "wx", "ts": 1.0})
    out = build_output(
        {"MARROW_CHANNEL": ""},
        payload={"session_id": "abc"},
        reader=reader,
    )
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: cli]"


def test_missing_last_active_handled() -> None:
    reader = _FakeReader(None)
    out = build_output(
        {"MARROW_CHANNEL": ""},
        payload={"session_id": "abc"},
        reader=reader,
    )
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: cli]"


def test_no_sid_skips_lookup() -> None:
    reader = _FakeReader({"sid": "abc", "channel": "wx", "ts": 1.0})
    out = build_output(
        {"MARROW_CHANNEL": ""},
        payload={"session_id": ""},
        reader=reader,
    )
    assert out["hookSpecificOutput"]["additionalContext"] == "[channel: cli]"


def test_previous_channel_none_when_no_sid() -> None:
    reader = _FakeReader({"sid": "abc", "channel": "wx", "ts": 1.0})
    assert _previous_channel("", reader=reader) == ""


def test_previous_channel_tolerates_read_error() -> None:
    class _Boom:
        def read(self) -> dict:
            raise RuntimeError("boom")

    assert _previous_channel("abc", reader=_Boom()) == ""


def test_stamp_last_active_writes_when_sid_present() -> None:
    writer = _FakeWriter()
    _stamp_last_active("abc", "cli", writer=writer)
    assert len(writer.calls) == 1
    sid, channel, ts = writer.calls[0]
    assert sid == "abc"
    assert channel == "cli"
    assert ts > 0


def test_stamp_last_active_noop_on_empty_sid() -> None:
    writer = _FakeWriter()
    _stamp_last_active("", "cli", writer=writer)
    assert writer.calls == []


def test_stamp_last_active_swallows_writer_error() -> None:
    class _Boom:
        def write(self, *_a, **_k) -> None:
            raise RuntimeError("boom")

    _stamp_last_active("abc", "cli", writer=_Boom())
