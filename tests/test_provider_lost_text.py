"""is_turn_event: classifies events that only occur inside a main-thread turn."""

from __future__ import annotations

from synapse_core.providers.cc import is_turn_event


def test_is_turn_event_true_for_main_thread_turn_events():
    for t in ("stream_event", "assistant", "user", "result"):
        assert is_turn_event({"type": t, "parent_tool_use_id": None}) is True
        # Real events always carry the key, but a missing key (absent in
        # synthetic/old events) still means "not a subagent".
        assert is_turn_event({"type": t}) is True


def test_is_turn_event_false_for_out_of_band_system_frames():
    for subtype in (
        "init",
        "status",
        "background_tasks_changed",
        "task_updated",
        "task_progress",
        "task_started",
    ):
        assert is_turn_event({"type": "system", "subtype": subtype}) is False


def test_is_turn_event_false_for_rate_limit_event():
    assert is_turn_event({"type": "rate_limit_event"}) is False


def test_is_turn_event_false_for_subagent_events():
    assert (
        is_turn_event({"type": "stream_event", "parent_tool_use_id": "toolu_x"})
        is False
    )
    assert (
        is_turn_event({"type": "assistant", "parent_tool_use_id": "toolu_x"})
        is False
    )
