"""Tests for HealthGate: boot/clean-shutdown/restart-detect cycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_core.health import HealthGate


@pytest.fixture()
def fake_clock():
    now = {"t": 1_700_000_000.0}

    def clock() -> float:
        return now["t"]

    clock.advance = lambda sec: now.__setitem__("t", now["t"] + sec)  # type: ignore
    return clock


def test_first_boot_does_not_announce(tmp_path: Path, fake_clock) -> None:
    gate = HealthGate(state_path=tmp_path / "health.json", clock=fake_clock)
    prev = gate.boot()
    assert prev == {}
    assert gate.should_announce_restart() is False


def test_first_boot_count_is_one(tmp_path: Path, fake_clock) -> None:
    path = tmp_path / "health.json"
    HealthGate(state_path=path, clock=fake_clock).boot()
    import json

    saved = json.loads(path.read_text())
    assert saved["boot_count"] == 1
    assert saved["last_boot_ts"] == 1_700_000_000.0


def test_clean_shutdown_then_second_boot_no_announce(
    tmp_path: Path, fake_clock
) -> None:
    path = tmp_path / "health.json"
    g1 = HealthGate(state_path=path, clock=fake_clock)
    g1.boot()
    fake_clock.advance(10)
    g1.stamp_clean_shutdown()

    fake_clock.advance(100)
    g2 = HealthGate(state_path=path, clock=fake_clock)
    g2.boot()
    assert g2.should_announce_restart() is False


def test_unclean_shutdown_then_second_boot_does_announce(
    tmp_path: Path, fake_clock
) -> None:
    path = tmp_path / "health.json"
    g1 = HealthGate(state_path=path, clock=fake_clock)
    g1.boot()
    # Skip stamp_clean_shutdown to simulate crash.
    fake_clock.advance(100)
    g2 = HealthGate(state_path=path, clock=fake_clock)
    g2.boot()
    assert g2.should_announce_restart() is True


def test_boot_count_increments_across_instances(tmp_path: Path, fake_clock) -> None:
    path = tmp_path / "health.json"
    HealthGate(state_path=path, clock=fake_clock).boot()
    fake_clock.advance(1)
    HealthGate(state_path=path, clock=fake_clock).boot()
    fake_clock.advance(1)
    HealthGate(state_path=path, clock=fake_clock).boot()

    import json

    saved = json.loads(path.read_text())
    assert saved["boot_count"] == 3


def test_state_persists_across_instances(tmp_path: Path, fake_clock) -> None:
    path = tmp_path / "health.json"
    g1 = HealthGate(state_path=path, clock=fake_clock)
    g1.boot()
    g1.stamp_clean_shutdown()

    g2 = HealthGate(state_path=path, clock=fake_clock)
    g2.boot()
    assert g2.should_announce_restart() is False


def test_should_announce_before_boot_is_false(tmp_path: Path, fake_clock) -> None:
    gate = HealthGate(state_path=tmp_path / "health.json", clock=fake_clock)
    # boot() never called.
    assert gate.should_announce_restart() is False


def test_corrupt_state_file_treated_as_first_boot(
    tmp_path: Path, fake_clock
) -> None:
    path = tmp_path / "health.json"
    path.write_text("{not json")
    gate = HealthGate(state_path=path, clock=fake_clock)
    prev = gate.boot()
    assert prev == {}
    assert gate.should_announce_restart() is False
