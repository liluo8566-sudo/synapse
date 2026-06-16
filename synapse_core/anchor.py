"""Time-anchor prefix for inbound bubbles before they reach the provider."""

from __future__ import annotations

from datetime import datetime

_WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# E-polish quote: when iLink delivers a `reference` field on the inbound msg,
# the bridge prepends '[quoting: "..."]' so cc sees what the user replied to.
_QUOTE_MAX = 80


def quote_prefix(quote_text: str | None) -> str:
    """Build '[quoting: "..."]' prefix; empty string when nothing to quote.

    Truncates over ``_QUOTE_MAX`` chars with '…' so a giant quoted message
    does not blow up the prompt budget.
    """
    if not quote_text:
        return ""
    body = quote_text.strip()
    if not body:
        return ""
    if len(body) > _QUOTE_MAX:
        body = body[:_QUOTE_MAX] + "…"
    return f'[quoting: "{body}"]'


def time_anchor(now: datetime, last_user_msg_ts: float) -> str:
    """Build '[time: YYYY-MM-DD Day HH:MM | gap: Nh]' prefix.

    First turn (last_user_msg_ts == 0) drops the '| gap: ...' segment.
    Gap < 1h reads as 'Xm' (integer minutes, no decimal).
    Gap >= 1h reads as 'X.Yh' (one decimal hour).
    """
    day = _WEEKDAY_SHORT[now.weekday()]
    stamp = now.strftime("%Y-%m-%d") + f" {day} " + now.strftime("%H:%M")
    if last_user_msg_ts <= 0:
        return f"[time: {stamp}]"
    gap_sec = now.timestamp() - last_user_msg_ts
    if gap_sec < 0:
        gap_sec = 0.0
    if gap_sec < 3600:
        gap_str = f"{int(gap_sec // 60)}m"
    else:
        gap_str = f"{gap_sec / 3600:.1f}h"
    return f"[time: {stamp} | gap: {gap_str}]"
