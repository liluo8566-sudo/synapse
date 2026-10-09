"""logging_config.py: the apscheduler max-instances noise filter.

check_flush runs every 0.5s with max_instances=1, so a flush still in
progress logs apscheduler's "skipped: maximum number of running instances
reached" warning every tick it overlaps — 110,938 of the last 200,000 lines
of the bridge log in production. The filter drops only that one line.
"""

from __future__ import annotations

import logging

import pytest

from synapse_core.logging_config import (
    _ApschedulerMaxInstancesFilter,
    configure_logging,
)


def _record(name: str, msg: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, __file__, 0, msg, None, None)


def test_drops_the_max_instances_warning_from_apscheduler_scheduler():
    """"apscheduler.scheduler" is the real logger name (BaseScheduler._logger)
    that emits this warning — not the "apscheduler" parent."""
    f = _ApschedulerMaxInstancesFilter()
    rec = _record(
        "apscheduler.scheduler",
        'Execution of job "check_flush (trigger: interval[0:00:00.500000], '
        'next run at: 2026-07-26 19:33:10 UTC)" skipped: maximum number of '
        "running instances reached (1)",
    )
    assert f.filter(rec) is False


def test_keeps_other_apscheduler_warnings():
    f = _ApschedulerMaxInstancesFilter()
    rec = _record("apscheduler.scheduler", "some other apscheduler warning")
    assert f.filter(rec) is True


def test_keeps_non_apscheduler_loggers_even_with_matching_text():
    """Scoped to the apscheduler family by logger name, not a blanket
    message-content filter that could swallow an unrelated logger's line
    that happens to contain the same words."""
    f = _ApschedulerMaxInstancesFilter()
    rec = _record(
        "httpx", "skipped: maximum number of running instances reached (1)")
    assert f.filter(rec) is True


@pytest.fixture
def _restore_root_logging():
    """configure_logging mutates the root logger's handlers/level globally —
    undo exactly what this test added so later tests in the same session see
    the state they started with."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    root.setLevel(before_level)
    for h in list(root.handlers):
        if h not in before_handlers:
            root.removeHandler(h)
            h.close()


def test_configure_logging_attaches_the_filter_to_both_handlers(
    tmp_path, _restore_root_logging
):
    configure_logging(tmp_path / "bridge.log")
    root = logging.getLogger()
    new_handlers = [h for h in root.handlers
                    if getattr(h, "_synapse_configured", False)]
    assert len(new_handlers) == 2
    for h in new_handlers:
        assert any(isinstance(flt, _ApschedulerMaxInstancesFilter)
                   for flt in h.filters)
