"""Tests for E-polish quote (inbound prefix + outbound quote-lite).

Inbound: when an iLink message carries a `reference` field, the bridge
prepends `[quoting: "..."]` to the user prompt via `anchor.time_anchor`.

Outbound: real ``ref_msg`` rendering was attempted live but WeChat does not
render the bubble as a quote-reply, so ``ILinkClient.send_text`` no longer
accepts a quote kwarg. The bridge instead prepends a visual fake-quote
bubble (``▎FRAGMENT``) ahead of the reply — covered by tests/test_loop_quote.py.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from unittest.mock import MagicMock

from synapse_core.anchor import quote_prefix, time_anchor
from synapse_wx.ilink.client import ILinkClient

# ── anchor: inbound quote prefix ─────────────────────────────────────


def test_quote_prefix_short() -> None:
    assert quote_prefix("hello world") == '[quoting: "hello world"]'


def test_quote_prefix_strips_whitespace() -> None:
    assert quote_prefix("  hi there  \n") == '[quoting: "hi there"]'


def test_quote_prefix_empty_returns_empty() -> None:
    assert quote_prefix("") == ""
    assert quote_prefix("   ") == ""
    assert quote_prefix(None) == ""  # type: ignore[arg-type]


def test_quote_prefix_truncates_long() -> None:
    out = quote_prefix("a" * 200)
    # Truncated to ≤80 char with ellipsis.
    assert out.startswith('[quoting: "')
    assert out.endswith('"]')
    assert "…" in out


def test_time_anchor_unchanged_no_quote_arg() -> None:
    # Backward compat: existing callers without quote_text still get the
    # plain anchor.
    now = datetime(2026, 6, 2, 14, 30)
    out = time_anchor(now, 0.0)
    assert out.startswith("[time:")
    assert "quoting" not in out


# ── outbound: send_text no quote kwarg, no ref_msg in payload ─────────


def _mk_client() -> tuple[ILinkClient, MagicMock]:
    client = ILinkClient()
    client.bot_token = "tok"
    client.base_url = "https://example.test"
    mock_http = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"ret": 0}
    mock_response.status_code = 200
    mock_http.post.return_value = mock_response
    client._client = mock_http
    return client, mock_http


def test_send_text_payload_has_no_ref_msg() -> None:
    """Outbound payload must never carry ref_msg — WeChat doesn't render it."""
    client, mock_http = _mk_client()
    client.send_text("user-1", "ctx-1", "hi")
    payload = mock_http.post.call_args.kwargs["json"]
    item0 = payload["msg"]["item_list"][0]
    assert "ref_msg" not in item0


def test_send_text_signature_drops_quote_inbound_item_kwarg() -> None:
    """quote_inbound_item is removed; quote_to never existed."""
    sig = inspect.signature(ILinkClient.send_text)
    assert "quote_inbound_item" not in sig.parameters
    assert "quote_to" not in sig.parameters
