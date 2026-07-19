"""Tool-in-flight idle liveness tests.

While a tool_use is awaiting its tool_result (e.g. a long-running MCP tool
call), the stream is legitimately silent — the hard stall threshold widens
from idle_hard_s to tool_idle_hard_s for that window only. Reuses the
scripted-stdout harness from test_provider_idle.py.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.providers.cc import ClaudeCodeProvider
from synapse_core.providers.errors import ProviderStallError
from tests.test_provider_idle import _ScriptedStdout


def _init() -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n"


def _assistant_tool_use(tool_id: str) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": tool_id, "name": "x", "input": {}}
                    ]
                },
            }
        )
        + "\n"
    )


def _user_tool_result(tool_id: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "ok",
                        }
                    ]
                },
            }
        )
        + "\n"
    )


def _result(text: str = "ok") -> str:
    return json.dumps({"type": "result", "result": text}) + "\n"


def _provider(stdout, *, idle_soft_s=0.15, idle_hard_s=0.3, tool_idle_hard_s=0.8):
    p = ClaudeCodeProvider(
        channel="test",
        cwd="/tmp",
        stderr_log=None,
        idle_soft_s=idle_soft_s,
        idle_hard_s=idle_hard_s,
        tool_idle_hard_s=tool_idle_hard_s,
    )
    fake = MagicMock()
    fake.stdin = MagicMock()
    fake.stdin.closed = False
    fake.stdout = stdout
    fake.stderr = MagicMock()
    fake.pid = 12345
    fake.poll.return_value = None
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = fake
        p.spawn()
    return p, fake


def test_pending_tool_ids_set_and_cleared():
    """tool_use marks the id pending; the matching tool_result clears it."""
    p, _ = _provider(
        _ScriptedStdout(
            [
                (0.0, _assistant_tool_use("tu1")),
                (0.0, _user_tool_result("tu1")),
                (0.0, _result()),
            ]
        )
    )
    gen = p.recv()
    ev = next(gen)
    assert ev["type"] == "assistant"
    assert p._pending_tool_ids == {"tu1"}
    ev = next(gen)
    assert ev["type"] == "user"
    assert p._pending_tool_ids == set()
    ev = next(gen)
    assert ev["type"] == "result"
    with pytest.raises(StopIteration):
        next(gen)


def test_silence_within_tool_idle_hard_s_survives_while_awaiting_tool():
    """A gap past idle_hard_s but under tool_idle_hard_s must NOT stall while
    a tool_use is awaiting its tool_result."""
    p, _ = _provider(
        _ScriptedStdout(
            [
                (0.0, _assistant_tool_use("tu1")),
                # 0.5s > idle_hard_s(0.3) but < tool_idle_hard_s(0.8).
                (0.5, _user_tool_result("tu1")),
                (0.0, _result()),
            ]
        ),
        idle_soft_s=0.15,
        idle_hard_s=0.3,
        tool_idle_hard_s=0.8,
    )
    out = list(p.recv())
    assert [e["type"] for e in out] == ["assistant", "user", "result"]


def test_stall_still_fires_past_tool_idle_hard_s_while_awaiting_tool():
    """Silence past tool_idle_hard_s while a tool is still pending must
    still raise ProviderStallError."""
    p, fake = _provider(
        _ScriptedStdout([(0.0, _assistant_tool_use("tu1")), (0.0, None)]),
        idle_soft_s=0.1,
        idle_hard_s=0.2,
        tool_idle_hard_s=0.4,
    )
    start = time.monotonic()
    with patch("synapse_core.providers.cc.os.getpgid", return_value=999), patch(
        "synapse_core.providers.cc.os.killpg"
    ) as killpg:
        with pytest.raises(ProviderStallError):
            list(p.recv())
    elapsed = time.monotonic() - start
    # Must survive past idle_hard_s(0.2) and fire near tool_idle_hard_s(0.4).
    assert 0.3 < elapsed < 2.0
    assert killpg.called
    assert p.alive is False


def test_stall_behaviour_unchanged_without_pending_tool():
    """No tool pending: the hard stall still fires at idle_hard_s (not
    tool_idle_hard_s), matching pre-existing behaviour."""
    p, fake = _provider(
        _ScriptedStdout([(0.0, _init()), (0.0, None)]),
        idle_soft_s=0.1,
        idle_hard_s=0.3,
        tool_idle_hard_s=5.0,
    )
    start = time.monotonic()
    with patch("synapse_core.providers.cc.os.getpgid", return_value=999), patch(
        "synapse_core.providers.cc.os.killpg"
    ) as killpg:
        with pytest.raises(ProviderStallError):
            list(p.recv())
    elapsed = time.monotonic() - start
    assert 0.2 < elapsed < 1.5
    assert killpg.called
