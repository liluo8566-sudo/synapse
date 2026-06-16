"""Bridge boot resume: MainLoop.start(boot_resume_sid=...) makes the first cc
spawn a --resume of the persisted sid so launchd restarts don't drop the user
onto a fresh session."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class _RecordingProvider:
    """Records factory kwargs + spawn call. Stays alive after spawn so start()
    sees a live provider and skips the second spawn path."""

    def __init__(self, model=None, resume_sid=None) -> None:
        self.spawn_kwargs: dict | None = None
        self.factory_model = model
        self.factory_resume_sid = resume_sid
        self.alive = False

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.spawn_kwargs = {"env": env}
        self.alive = True

    def send(self, _msg: str) -> None:  # pragma: no cover — start() doesn't send
        pass

    def recv(self):  # pragma: no cover
        if False:
            yield {}
        return

    def cancel(self) -> None:  # pragma: no cover
        pass

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class _SilentILink:
    def poll_messages(self) -> list[dict]:
        return []

    def send_text(self, *_a, **_k) -> bool:
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


@pytest.fixture()
def env(tmp_path: Path):
    last_factory: dict = {}

    def factory(model=None, resume_sid=None):
        prov = _RecordingProvider(model=model, resume_sid=resume_sid)
        last_factory["prov"] = prov
        return prov

    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    loop = MainLoop(
        ilink=_SilentILink(),
        provider_factory=factory,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    yield loop, state, last_factory
    loop.stop()


def test_start_with_boot_resume_sid_passes_to_factory(env) -> None:
    loop, state, last = env
    loop.start(boot_resume_sid="resume-me-123")
    prov = last["prov"]
    assert prov.factory_resume_sid == "resume-me-123"
    assert prov.spawn_kwargs is not None  # spawn() called
    # state.session_id seeded so tick() treats it as the live session
    # without waiting for cc's `system{init}` echo.
    assert state.session_id == "resume-me-123"


def test_start_without_boot_resume_sid_uses_fresh_factory(env) -> None:
    loop, state, last = env
    loop.start()
    prov = last["prov"]
    assert prov.factory_resume_sid is None
    assert state.session_id is None


def test_start_does_not_overwrite_existing_session_id(env) -> None:
    """If the bridge already had a session_id (e.g. mid-life respawn), don't
    clobber it with the boot resume sid."""
    loop, state, last = env
    state.session_id = "already-here"
    loop.start(boot_resume_sid="resume-me-123")
    assert state.session_id == "already-here"
