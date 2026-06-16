"""Tests for /effort <low|medium|high|xhigh|max|ultracode|auto>.

Maps the level verbatim to cc's `--effort <level>` flag (cc 2.1.159+), set on
the next provider swap. Persists in BridgeState.effort_level (default "high"
= WeChat default).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.providers.cc import ClaudeCodeProvider
from synapse_core.state import BridgeState


def _noop(*_a, **_k) -> None:
    return None


def _make_registry(state: BridgeState | None = None) -> tuple[Registry, BridgeState]:
    s = state if state is not None else BridgeState()
    ctx = CommandContext(
        state=s,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
    )
    return Registry(ctx), s


# ── /effort dispatch ──────────────────────────────────────────────


def test_effort_default_high() -> None:
    _, s = _make_registry()
    assert s.effort_level == "high"


def test_effort_low_sets_level() -> None:
    reg, s = _make_registry()
    verdict, reply = reg.dispatch("/effort low")
    assert verdict == "handled"
    assert s.effort_level == "low"
    assert reply is not None and "low" in reply.lower()


def test_effort_medium_sets_level() -> None:
    reg, s = _make_registry()
    reg.dispatch("/effort medium")
    assert s.effort_level == "medium"


def test_effort_high_sets_level() -> None:
    s = BridgeState()
    s.effort_level = "low"
    reg, s = _make_registry(s)
    reg.dispatch("/effort high")
    assert s.effort_level == "high"


def test_effort_xhigh_sets_level() -> None:
    reg, s = _make_registry()
    reg.dispatch("/effort xhigh")
    assert s.effort_level == "xhigh"


def test_effort_max_sets_level() -> None:
    reg, s = _make_registry()
    reg.dispatch("/effort max")
    assert s.effort_level == "max"


def test_effort_ultracode_sets_level() -> None:
    reg, s = _make_registry()
    reg.dispatch("/effort ultracode")
    assert s.effort_level == "ultracode"


def test_effort_auto_sets_level() -> None:
    reg, s = _make_registry()
    reg.dispatch("/effort auto")
    assert s.effort_level == "auto"


def test_effort_no_arg_returns_usage() -> None:
    reg, _ = _make_registry()
    verdict, reply = reg.dispatch("/effort")
    assert verdict == "handled"
    assert reply is not None
    assert "low" in reply.lower() and "ultracode" in reply.lower()


def test_effort_bad_arg_returns_error() -> None:
    reg, s = _make_registry()
    verdict, reply = reg.dispatch("/effort weird")
    assert verdict == "handled"
    assert reply is not None
    # State unchanged on bad input.
    assert s.effort_level == "high"


def test_effort_legacy_off_rejected() -> None:
    # "off" was a legacy alias; new spec has no off. Bad arg → error, no change.
    reg, s = _make_registry()
    verdict, reply = reg.dispatch("/effort off")
    assert verdict == "handled"
    # Bad input renders the usage hint (not [error] prefix).
    assert "🐮🐴" in (reply or "")
    assert s.effort_level == "high"


# ── provider-side flag wiring ─────────────────────────────────────


def _cc_provider(**kwargs) -> ClaudeCodeProvider:
    params = {"cwd": "/tmp", "channel": "test", "stderr_log": None}
    params.update(kwargs)
    return ClaudeCodeProvider(**params)


def test_effort_level_passed_to_cc() -> None:
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = MagicMock()
        fake.stdin = MagicMock()
        fake.stdin.closed = False
        fake.stdout = iter([])
        fake.poll.return_value = None
        Popen.return_value = fake

        p = _cc_provider(model="claude-opus-4-6[1m]", effort_level="high")
        p.spawn()
        cmd = Popen.call_args[0][0]
        # Must appear as `--effort high` (two tokens, not `--effort=high`).
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"


def test_effort_level_omitted_when_none() -> None:
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = MagicMock()
        fake.stdin = MagicMock()
        fake.stdin.closed = False
        fake.stdout = iter([])
        fake.poll.return_value = None
        Popen.return_value = fake

        p = _cc_provider(model="claude-opus-4-6[1m]", effort_level=None)
        p.spawn()
        cmd = Popen.call_args[0][0]
        assert "--effort" not in cmd


def test_effort_legacy_thinking_budget_flag_gone() -> None:
    """The deprecated --thinking-budget flag must never appear."""
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        fake = MagicMock()
        fake.stdin = MagicMock()
        fake.stdin.closed = False
        fake.stdout = iter([])
        fake.poll.return_value = None
        Popen.return_value = fake

        p = _cc_provider(model="claude-opus-4-6[1m]", effort_level="high")
        p.spawn()
        joined = " ".join(Popen.call_args[0][0])
        assert "--thinking-budget" not in joined
