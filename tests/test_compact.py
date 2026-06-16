"""Tests for E-polish /compact.

Strategy: pipe `/compact` literally to the live cc subprocess's stdin first.
cc accepts that slash natively. Fallback (when provider can't pipe a raw
slash) writes an acknowledgement so the caller knows the path was attempted.
"""

from __future__ import annotations

from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


class _FakeProvider:
    """Captures send_raw / send_user_text calls."""

    def __init__(self, supports_raw: bool = True) -> None:
        self.supports_raw = supports_raw
        self.raw_calls: list[str] = []

    def send_raw_user_text(self, text: str) -> None:
        if not self.supports_raw:
            raise NotImplementedError("provider does not support raw stdin pipe")
        self.raw_calls.append(text)


def _make_ctx(state: BridgeState, **overrides):
    base = dict(
        state=state,
        swap_provider=lambda *_a, **_kw: None,
        close_provider=lambda: None,
        forget_session=lambda: None,
    )
    base.update(overrides)
    return CommandContext(**base)  # type: ignore[arg-type]


def test_compact_no_sid_returns_friendly() -> None:
    state = BridgeState(session_id=None)
    reg = Registry(_make_ctx(state))
    verdict, reply = reg.dispatch("/compact")
    assert verdict == "handled"
    assert reply is not None
    assert "没东西压" in reply


def test_compact_pipes_slash_when_handler_wired() -> None:
    state = BridgeState(session_id="sid-abc", model="claude-opus-4-7[1m]")
    fake = _FakeProvider()
    called: list[bool] = []

    def compact_handler() -> str:
        called.append(True)
        fake.send_raw_user_text("/compact")
        return "[compact] piped /compact to cc"

    ctx = _make_ctx(state, compact_handler=compact_handler)
    reg = Registry(ctx)
    verdict, reply = reg.dispatch("/compact")
    assert verdict == "handled"
    assert called == [True]
    assert fake.raw_calls == ["/compact"]
    assert reply is not None
    assert "compact" in reply.lower()


def test_compact_default_handler_is_noop_with_message() -> None:
    # No compact_handler wired => still handled, returns informative message
    # rather than crashing.
    state = BridgeState(session_id="sid-x")
    reg = Registry(_make_ctx(state))
    verdict, reply = reg.dispatch("/compact")
    assert verdict == "handled"
    assert reply is not None


def test_compact_handler_failure_falls_back() -> None:
    # If the pipe path raises, the handler returns a fallback ack rather
    # than crashing the bridge.
    state = BridgeState(session_id="sid-y")

    def compact_handler() -> str:
        raise RuntimeError("stdin pipe closed")

    ctx = _make_ctx(state, compact_handler=compact_handler)
    reg = Registry(ctx)
    verdict, reply = reg.dispatch("/compact")
    assert verdict == "handled"
    assert reply is not None
    # Reply should indicate failure, not blow up
    assert "压缩" in reply


# ── provider raw-pipe API ────────────────────────────────────────


def test_provider_send_raw_user_text_writes_user_frame() -> None:
    """ClaudeCodeProvider.send_raw_user_text pipes the literal text as a
    user-role frame (cc parses leading `/` server-side)."""
    import io
    import json
    from unittest.mock import MagicMock, patch

    from synapse_core.providers.cc import ClaudeCodeProvider

    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = MagicMock()
        fake.stdin = MagicMock()
        fake.stdin.closed = False
        fake.stdout = iter([])
        fake.stderr = io.StringIO("")
        fake.poll.return_value = None
        Popen.return_value = fake

        p = ClaudeCodeProvider(cwd="/tmp", channel="test", stderr_log=None)
        p.spawn()
        p.send_raw_user_text("/compact")
        written = fake.stdin.write.call_args[0][0]
        frame = json.loads(written.rstrip("\n"))
        assert frame["type"] == "user"
        assert frame["message"]["content"] == "/compact"
