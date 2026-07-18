"""Provider idle-liveness tests: soft check, hard kill, timer reset.

Uses a fake stdout pipe that yields lines on a controlled schedule so the
provider's silence timer can be exercised without a real subprocess. Idle
thresholds are set tiny (fractions of a second) to keep the suite fast.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.providers.cc import ClaudeCodeProvider
from synapse_core.providers.errors import ProviderDeadError, ProviderStallError


class _ScriptedStdout:
    """Iterable stdout: yields (delay, line) items, sleeping before each line.

    A line of None means "block forever" (simulates a wedged-but-alive cc).
    Iteration runs in the provider's reader thread.
    """

    def __init__(self, script: list[tuple[float, str | None]]) -> None:
        self._script = script
        self._i = 0
        self._blocked = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._script):
            raise StopIteration
        delay, line = self._script[self._i]
        self._i += 1
        time.sleep(delay)
        if line is None:
            # Wedge: block until the process is killed (thread is daemon).
            self._blocked.set()
            while True:
                time.sleep(0.05)
        return line

    def close(self) -> None:
        return None


def _provider(stdout, *, alive=True, idle_soft_s=0.2, idle_hard_s=0.6):
    p = ClaudeCodeProvider(
        channel="test",
        cwd="/tmp",
        stderr_log=None,
        idle_soft_s=idle_soft_s,
        idle_hard_s=idle_hard_s,
    )
    fake = MagicMock()
    fake.stdin = MagicMock()
    fake.stdin.closed = False
    fake.stdout = stdout
    fake.stderr = MagicMock()
    fake.pid = 12345
    fake.poll.return_value = None if alive else 0
    with patch("synapse_core.providers.cc.subprocess.Popen") as Popen:
        Popen.return_value = fake
        p.spawn()
    return p, fake


def _result(text="ok"):
    import json
    return json.dumps({"type": "result", "result": text}) + "\n"


def test_silent_but_alive_survives_past_soft_check():
    """A gap longer than idle_soft_s but with a live process must NOT raise:
    the event arrives afterward and the turn completes."""
    import json
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n"
    # 0.35s > idle_soft_s(0.2) but < idle_hard_s(0.6): soft check sees alive,
    # keeps waiting, then the result arrives.
    p, _ = _provider(_ScriptedStdout([(0.0, init), (0.35, _result())]))
    out = list(p.recv())
    assert out[-1]["type"] == "result"


def test_dead_process_detected_at_soft_check():
    """If the process is dead when the soft check fires, raise ProviderDead
    without waiting for the hard deadline."""
    # No lines ever; process reports dead (poll != None).
    p, _ = _provider(_ScriptedStdout([]), alive=False, idle_soft_s=0.15, idle_hard_s=5.0)
    start = time.monotonic()
    with pytest.raises(ProviderDeadError):
        list(p.recv())
    # Must have raised near the soft check, far before the 5s hard deadline.
    assert time.monotonic() - start < 1.0


def test_hard_kill_fires_at_idle_hard_s():
    """A wedged-but-alive process is killed at idle_hard_s and raises
    ProviderStallError (a ProviderDeadError subclass)."""
    p, fake = _provider(
        _ScriptedStdout([(0.0, None)]), idle_soft_s=0.15, idle_hard_s=0.4
    )
    start = time.monotonic()
    with patch("synapse_core.providers.cc.os.getpgid", return_value=999), \
            patch("synapse_core.providers.cc.os.killpg") as killpg:
        with pytest.raises(ProviderStallError):
            list(p.recv())
    elapsed = time.monotonic() - start
    assert 0.3 < elapsed < 2.0
    assert killpg.called
    assert p.alive is False


def test_event_arrival_resets_the_timer():
    """Two gaps each just under idle_hard_s but summing over it must survive:
    the second gap's clock restarts when the first event lands."""
    import json
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n"
    asst = json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    # idle_hard_s = 0.5; two 0.35s gaps (total 0.7 > 0.5) but each < 0.5.
    p, _ = _provider(
        _ScriptedStdout([(0.0, init), (0.35, asst), (0.35, _result())]),
        idle_soft_s=0.2,
        idle_hard_s=0.5,
    )
    out = list(p.recv())
    assert [e["type"] for e in out] == ["system", "assistant", "result"]


def test_stall_error_is_provider_dead_subclass():
    """ProviderStallError must be catchable as ProviderDeadError so existing
    death-handling paths work unchanged."""
    assert issubclass(ProviderStallError, ProviderDeadError)
