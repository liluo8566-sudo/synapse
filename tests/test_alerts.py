"""Tests for AlertSink: disk write, naming, chmod, list_recent, marrow_repo_cmd."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from synapse_core.alerts import AlertSink


@pytest.fixture()
def fake_clock():
    now = {"t": 1_700_000_000}

    def clock() -> float:
        return float(now["t"])

    clock.advance = lambda sec: now.__setitem__("t", now["t"] + sec)  # type: ignore
    return clock


def test_write_creates_file_with_expected_name(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, clock=fake_clock)
    p = sink.write("critical", "provider_dead", "boom", source="loop._drain_recv")
    assert p.exists()
    assert p.name == "1700000000_critical_provider_dead.txt"
    assert p.parent == tmp_path


def test_write_body_is_valid_json_one_liner(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, clock=fake_clock)
    p = sink.write("warn", "ilink_retry_exhausted", "timeout", source="ilink")
    body = p.read_text()
    assert body.endswith("\n")
    data = json.loads(body)
    assert data == {
        "ts": 1_700_000_000,
        "severity": "warn",
        "kind": "ilink_retry_exhausted",
        "fingerprint": "ilink_retry_exhausted",
        "message": "timeout",
        "source": "ilink",
    }


def test_write_chmods_to_600(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, clock=fake_clock)
    p = sink.write("warn", "kind", "msg")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


def test_unknown_severity_coerced_to_warn(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, clock=fake_clock)
    p = sink.write("info", "weird", "x")
    data = json.loads(p.read_text())
    assert data["severity"] == "warn"


def test_alerts_dir_created_on_init(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "alerts"
    AlertSink(alerts_dir=nested)
    assert nested.is_dir()


def test_list_recent_filters_by_mtime(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, clock=fake_clock)
    p1 = sink.write("warn", "k1", "old")
    fake_clock.advance(100)
    p2 = sink.write("warn", "k2", "new")
    # Force older mtime on p1.
    os.utime(p1, (1_700_000_000, 1_700_000_000))
    os.utime(p2, (1_700_000_100, 1_700_000_100))
    rows = sink.list_recent(since_ts=1_700_000_050)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["k2"]


def test_list_recent_returns_all_when_since_zero(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, clock=fake_clock)
    sink.write("warn", "a", "x")
    fake_clock.advance(1)
    sink.write("critical", "b", "y")
    rows = sink.list_recent()
    assert {r["kind"] for r in rows} == {"a", "b"}


def test_empty_marrow_repo_cmd_no_spawn(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, marrow_repo_cmd="", clock=fake_clock)
    with patch("synapse_core.alerts.subprocess.Popen") as popen:
        sink.write("warn", "k", "m")
        popen.assert_not_called()


def test_none_marrow_repo_cmd_no_spawn(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(alerts_dir=tmp_path, marrow_repo_cmd=None, clock=fake_clock)
    with patch("synapse_core.alerts.subprocess.Popen") as popen:
        sink.write("warn", "k", "m")
        popen.assert_not_called()


def test_marrow_repo_cmd_spawns_with_args_list(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(
        alerts_dir=tmp_path,
        marrow_repo_cmd="python -m marrow.repo add_alert",
        clock=fake_clock,
    )
    with patch("synapse_core.alerts.subprocess.Popen") as popen:
        sink.write("critical", "provider_dead", "boom", source="loop")
    popen.assert_called_once()
    args, kwargs = popen.call_args
    argv = args[0]
    assert isinstance(argv, list)
    assert argv[0] == "python"
    assert argv[:4] == ["python", "-m", "marrow.repo", "add_alert"]
    # severity / kind / fingerprint positional, then --source and --message flags
    assert argv[4:7] == ["critical", "provider_dead", "provider_dead"]
    assert "--source" in argv and argv[argv.index("--source") + 1] == "loop"
    assert "--message" in argv and argv[argv.index("--message") + 1] == "boom"
    # No shell=True ever.
    assert kwargs.get("shell", False) is False


def test_marrow_popen_oserror_swallowed(tmp_path: Path, fake_clock) -> None:
    sink = AlertSink(
        alerts_dir=tmp_path,
        marrow_repo_cmd="python -m marrow.repo add_alert",
        clock=fake_clock,
    )
    with patch(
        "synapse_core.alerts.subprocess.Popen", side_effect=OSError("nope")
    ):
        # Must not raise — file write is source of truth.
        p = sink.write("warn", "k", "m")
        assert p.exists()
