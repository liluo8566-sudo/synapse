"""Tests for time_anchor — first-turn, minute/hour gap formatting, weekday case."""

from __future__ import annotations

from datetime import datetime

from synapse_core.anchor import time_anchor


def test_first_turn_no_gap_segment() -> None:
    # 2026-06-02 is a Tuesday
    now = datetime(2026, 6, 2, 2, 48)
    out = time_anchor(now, last_user_msg_ts=0.0)
    assert out == "[time: 2026-06-02 Tue 02:48]"
    assert "gap" not in out


def test_five_minute_gap_format() -> None:
    now = datetime(2026, 6, 2, 2, 50)
    last = datetime(2026, 6, 2, 2, 45).timestamp()
    out = time_anchor(now, last)
    assert out == "[time: 2026-06-02 Tue 02:50 | gap: 5m]"


def test_ninety_minute_gap_one_decimal_hour() -> None:
    now = datetime(2026, 6, 2, 4, 0)
    last = datetime(2026, 6, 2, 2, 30).timestamp()
    out = time_anchor(now, last)
    assert "gap: 1.5h" in out


def test_four_hour_gap() -> None:
    now = datetime(2026, 6, 2, 6, 50)
    last = datetime(2026, 6, 2, 2, 50).timestamp()
    out = time_anchor(now, last)
    assert out == "[time: 2026-06-02 Tue 06:50 | gap: 4.0h]"


def test_weekday_short_form() -> None:
    # 2026-06-02 = Tue, not Tuesday
    out = time_anchor(datetime(2026, 6, 2, 12, 0), 0.0)
    assert " Tue " in out
    assert "Tuesday" not in out
    # Sunday check
    out_sun = time_anchor(datetime(2026, 6, 7, 12, 0), 0.0)
    assert " Sun " in out_sun


def test_gap_under_one_minute_reads_zero_min() -> None:
    now = datetime(2026, 6, 2, 3, 0, 30)
    last = datetime(2026, 6, 2, 3, 0, 0).timestamp()
    out = time_anchor(now, last)
    assert "gap: 0m" in out
