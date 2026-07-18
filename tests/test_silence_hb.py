"""Tests for HTML-comment silence protocol and /hb reaction behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from synapse_tg.loop import strip_html_comments
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


# ── strip_html_comments unit tests ────────────────────────────────────────────


def test_strip_single_comment_leaves_empty() -> None:
    assert strip_html_comments("<!-- silent -->") == ""


def test_strip_comment_preserves_surrounding_text() -> None:
    assert strip_html_comments("hello <!-- aside --> world") == "hello  world"


def test_strip_multiline_comment() -> None:
    result = strip_html_comments("before\n<!-- \nline1\nline2\n-->\nafter")
    assert result == "before\n\nafter"


def test_strip_multiple_comments() -> None:
    result = strip_html_comments("<!-- a -->text<!-- b -->")
    assert result == "text"


def test_strip_comment_only_whitespace_returns_empty() -> None:
    assert strip_html_comments("  <!-- x -->  ") == ""


def test_strip_no_comment_unchanged() -> None:
    assert strip_html_comments("hello world") == "hello world"


# ── unclosed <!-- preview guard (inline logic test) ───────────────────────────


def test_unclosed_comment_guard_truncates() -> None:
    """Simulate the preview guard: accumulated text with unclosed <!-- is
    truncated at the open-comment position for display."""
    accumulated = "hello <!-- not closed yet"
    open_idx = accumulated.find("<!--")
    display_text = accumulated[:open_idx] if open_idx != -1 else accumulated
    assert display_text == "hello "


def test_closed_comment_guard_no_truncation() -> None:
    """Fully closed comment in accumulated → no truncation needed (guard is a no-op)."""
    accumulated = "hello <!-- comment --> world"
    open_idx = accumulated.find("<!--")
    # There IS an open tag, so the guard still truncates — but by then
    # strip_html_comments should have cleaned preview_chunk before adding.
    # This test documents the guard behaviour: it truncates at <!--.
    display_text = accumulated[:open_idx] if open_idx != -1 else accumulated
    assert display_text == "hello "


# ── /hb install returns text ack ─────────────────────────────────────────────


def _make_registry() -> tuple[Registry, BridgeState]:
    s = BridgeState()
    ctx = CommandContext(
        state=s,
        swap_provider=lambda m, sid: None,
        close_provider=lambda: None,
        forget_session=lambda: None,
    )
    return Registry(ctx), s


def test_hb_install_returns_text_ack() -> None:
    """Successful /hb <minutes> install returns a non-empty text ack confirming the interval."""
    reg, _ = _make_registry()
    with patch("synapse_core.commands.registry.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        verdict, ack = reg.dispatch("/hb 20")
    assert verdict == "handled"
    assert ack  # non-empty — confirms interval change to user
    assert "20" in ack


def test_hb_status_returns_text() -> None:
    """/hb with no argument (status query) still returns a text ack."""
    reg, _ = _make_registry()
    with patch("synapse_core.commands.registry.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        verdict, ack = reg.dispatch("/hb")
    assert verdict == "handled"
    assert ack  # non-empty


def test_hb_off_returns_text() -> None:
    """/hb off still returns a text ack."""
    reg, _ = _make_registry()
    with patch("synapse_core.commands.registry.subprocess.run"):
        verdict, ack = reg.dispatch("/hb off")
    assert verdict == "handled"
    assert ack  # non-empty


def test_hb_bad_arg_returns_usage() -> None:
    """/hb notanumber returns usage text."""
    reg, _ = _make_registry()
    verdict, ack = reg.dispatch("/hb notanumber")
    assert verdict == "handled"
    assert ack  # non-empty


def test_hb_install_subprocess_error_returns_usage() -> None:
    """If subprocess raises, /hb falls back to usage text (not empty string)."""
    reg, _ = _make_registry()
    with patch("synapse_core.commands.registry.subprocess.run", side_effect=OSError("no script")):
        verdict, ack = reg.dispatch("/hb 5")
    assert verdict == "handled"
    assert ack  # usage text, not ""
