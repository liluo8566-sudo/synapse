"""Tests for SleepWakeObserver. Mocks pyobjc via sys.modules injection.

The lazy-import lives inside start(); we patch sys.modules entries before
calling start() so the test runs on non-mac CI without pyobjc installed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse_core.alerts import AlertSink
from synapse_wx.sleep import SleepWakeObserver


class _FakeNSObject:
    """Minimal NSObject stand-in so the bridge subclass can inherit + alloc().init()."""

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


def _install_fake_pyobjc(monkeypatch, center: MagicMock) -> None:
    fake_workspace = MagicMock()
    fake_workspace.sharedWorkspace.return_value.notificationCenter.return_value = (
        center
    )
    fake_cocoa = types.ModuleType("Cocoa")
    fake_cocoa.NSWorkspace = fake_workspace  # type: ignore[attr-defined]

    fake_foundation = types.ModuleType("Foundation")
    fake_foundation.NSObject = _FakeNSObject  # type: ignore[attr-defined]

    fake_objc = types.ModuleType("objc")
    fake_objc.python_method = lambda f: f  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Cocoa", fake_cocoa)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    monkeypatch.setitem(sys.modules, "objc", fake_objc)


@pytest.fixture()
def alerts(tmp_path: Path) -> AlertSink:
    return AlertSink(alerts_dir=tmp_path / "alerts")


def test_start_registers_observer_for_sleep_and_wake(monkeypatch, alerts) -> None:
    center = MagicMock()
    _install_fake_pyobjc(monkeypatch, center)

    obs = SleepWakeObserver(
        will_sleep=lambda: None, did_wake=lambda: None, alerts=alerts
    )
    obs.start()

    calls = center.addObserver_selector_name_object_.call_args_list
    assert len(calls) == 2
    names = [c.args[2] for c in calls]
    selectors = [c.args[1] for c in calls]
    assert "NSWorkspaceWillSleepNotification" in names
    assert "NSWorkspaceDidWakeNotification" in names
    assert "willSleep:" in selectors
    assert "didWake:" in selectors


def test_stop_removes_observer(monkeypatch, alerts) -> None:
    center = MagicMock()
    _install_fake_pyobjc(monkeypatch, center)

    obs = SleepWakeObserver(
        will_sleep=lambda: None, did_wake=lambda: None, alerts=alerts
    )
    obs.start()
    obs.stop()
    center.removeObserver_.assert_called_once()


def test_start_is_idempotent(monkeypatch, alerts) -> None:
    center = MagicMock()
    _install_fake_pyobjc(monkeypatch, center)

    obs = SleepWakeObserver(
        will_sleep=lambda: None, did_wake=lambda: None, alerts=alerts
    )
    obs.start()
    obs.start()
    # only the first start registers.
    assert center.addObserver_selector_name_object_.call_count == 2


def test_stop_without_start_is_noop(monkeypatch, alerts) -> None:
    center = MagicMock()
    _install_fake_pyobjc(monkeypatch, center)

    obs = SleepWakeObserver(
        will_sleep=lambda: None, did_wake=lambda: None, alerts=alerts
    )
    obs.stop()
    center.removeObserver_.assert_not_called()


def test_importerror_writes_warn_alert(monkeypatch, alerts) -> None:
    # Force ImportError by setting None entries in sys.modules.
    for name in ("Cocoa", "Foundation", "objc"):
        monkeypatch.setitem(sys.modules, name, None)

    obs = SleepWakeObserver(
        will_sleep=lambda: None, did_wake=lambda: None, alerts=alerts
    )
    obs.start()  # must not raise

    rows = alerts.list_recent()
    assert len(rows) == 1
    assert rows[0]["severity"] == "warn"
    assert rows[0]["kind"] == "pyobjc_missing"


def test_importerror_no_alert_when_alerts_none(monkeypatch) -> None:
    for name in ("Cocoa", "Foundation", "objc"):
        monkeypatch.setitem(sys.modules, name, None)

    obs = SleepWakeObserver(
        will_sleep=lambda: None, did_wake=lambda: None, alerts=None
    )
    obs.start()  # must not raise even without alerts
