"""B11: on idle fire, close provider first (cc SessionEnd writes archive_events),
then popen-detach sessionend_async. state.session_id is NOT cleared.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker

HOUR = 3600


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


def _make_jsonl(projects_dir: Path, project: str, sid: str, mtime: float) -> Path:
    p = projects_dir / project
    p.mkdir(parents=True, exist_ok=True)
    jsonl = p / f"{sid}.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (mtime, mtime))
    return jsonl


def _mock_proc(poll_rc: int | None = None) -> MagicMock:
    m = MagicMock()
    m.poll.return_value = poll_rc
    return m


@pytest.fixture()
def env(tmp_path: Path):
    projects = tmp_path / "projects"
    markers = tmp_path / "markers"
    audit = tmp_path / "audit.log"
    err_log = tmp_path / "sessionend.err.log"
    state = tmp_path / "sessions.json"
    projects.mkdir()
    tracker = SessionTracker(state_path=state)
    return {
        "tmp": tmp_path,
        "projects": projects,
        "markers": markers,
        "audit": audit,
        "err_log": err_log,
        "tracker": tracker,
    }


def _build_loop(
    env, command_template: str, clock: FakeClock, pre_spawn_hook=None
) -> IdleFireLoop:
    return IdleFireLoop(
        sessions=env["tracker"],
        command_template=command_template,
        marker_dir=env["markers"],
        audit_log=env["audit"],
        sessionend_err_log=env["err_log"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
        spawn_probe_sec=0.0,
        pre_spawn_hook=pre_spawn_hook,
    )


def test_pre_spawn_hook_invoked_before_subprocess_spawn(env) -> None:
    """On fire, pre_spawn_hook(sid) runs BEFORE subprocess.Popen for sessionend_async."""
    clock = FakeClock()
    sid = "sid-b11a0001"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    call_order: list[str] = []

    def hook(s: str) -> None:
        call_order.append(f"hook:{s}")

    def popen_side_effect(*_a, **_kw):
        call_order.append("popen")
        return _mock_proc()

    loop = _build_loop(env, "python -m marrow.sessionend_async --sid {sid}", clock, hook)
    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen", side_effect=popen_side_effect
    ):
        fired = loop.tick_once()

    assert fired == [sid]
    assert call_order == [f"hook:{sid}", "popen"], call_order


def test_pre_spawn_hook_runs_with_empty_template(env) -> None:
    """Even with audit-only mode (no popen), hook still runs so cc gets closed."""
    clock = FakeClock()
    sid = "sid-b11a0002"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 8 * HOUR)

    seen: list[str] = []
    loop = _build_loop(env, "", clock, pre_spawn_hook=lambda s: seen.append(s))
    with patch("synapse_core.sessionend.idle.subprocess.Popen") as popen:
        fired = loop.tick_once()

    assert fired == [sid]
    assert seen == [sid]
    popen.assert_not_called()


def test_session_tracker_sid_retained_after_fire(env) -> None:
    """B11 critical: SessionTracker entry must NOT be removed by idle fire."""
    clock = FakeClock()
    sid = "sid-b11a0003"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async --sid {sid}", clock,
                       pre_spawn_hook=lambda _s: None)
    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()
    ):
        loop.tick_once()

    # sid must still be tracked for next-inbound resume.
    assert env["tracker"].get("u1") == sid


def test_pre_spawn_hook_exception_does_not_block_spawn(env) -> None:
    """If provider close throws, we still proceed to popen sessionend_async."""
    clock = FakeClock()
    sid = "sid-b11a0004"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    def bad_hook(_s: str) -> None:
        raise RuntimeError("close blew up")

    loop = _build_loop(env, "python -m marrow.sessionend_async --sid {sid}", clock, bad_hook)
    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()
    ) as popen:
        fired = loop.tick_once()

    assert fired == [sid]
    popen.assert_called_once()


def test_no_hook_keeps_legacy_behaviour(env) -> None:
    """Backwards compat: if pre_spawn_hook is None, fire path unchanged."""
    clock = FakeClock()
    sid = "sid-b11a0005"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async --sid {sid}", clock,
                       pre_spawn_hook=None)
    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()
    ) as popen:
        fired = loop.tick_once()

    assert fired == [sid]
    popen.assert_called_once()
