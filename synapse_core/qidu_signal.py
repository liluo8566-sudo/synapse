"""QiduSignalPoller: poll qidu book-server for pending highlight/annotation
signals, render to inject-able text.

Separate from QiduParser (book parsing, flock single-instance): this module
routes signal delivery by last_active (which bridge the user is on). No
flock — the server's /signal/consume is at-most-once, so a concurrent poll
from the "wrong" bridge just races and loses, no double-injection either
way.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

from synapse_core import last_active

logger = logging.getLogger(__name__)

DEFAULT_LAST_ACTIVE_PATH = Path.home() / ".config" / "marrow" / "last_active.json"
_MAX_CONSECUTIVE_FAILURES = 10

_DEFAULT_USER_NAME = "用户"

_TEMPLATES = {
    "highlight": (
        "[reading] {user_name}在《{book_title}》({chapter_title}) 划了一段话:\n"
        "「{quoted_text}」\n"
        "(book_id={book_id}, highlight_id={highlight_id} — 想批注就用 book_annotate)"
    ),
    "annotation": (
        "[reading] {user_name}在《{book_title}》划线并写了批注:\n"
        "原文:「{quoted_text}」\n"
        "批注:「{annotation_text}」\n"
        "(book_id={book_id}, highlight_id={highlight_id}, annotation_id={annotation_id})"
    ),
    "reply": (
        "[reading] {user_name}在《{book_title}》的批注 thread 里回复了你:\n"
        "原文:「{quoted_text}」\n"
        "她说:「{annotation_text}」\n"
        "(book_id={book_id}, highlight_id={highlight_id}, 回复请带 parent_id={annotation_id})"
    ),
}


def render_signal(event_type: str, payload: dict, user_name: str = _DEFAULT_USER_NAME) -> str | None:
    """Render one signal payload to injection text. Unknown event_type or
    missing field → None (skip silently, logged)."""
    template = _TEMPLATES.get(event_type)
    if template is None:
        logger.warning("unknown signal event_type: %s", event_type)
        return None
    try:
        return template.format(user_name=user_name or _DEFAULT_USER_NAME, **payload)
    except KeyError as e:
        logger.warning("signal payload missing field %s for event_type=%s", e, event_type)
        return None


class QiduSignalPoller:
    """Fetch pending signals from qidu book-server, render to inject-able text.

    Not flock-guarded (see module docstring) — unlike QiduParser which does
    single-instance book parsing.
    """

    def __init__(
        self,
        api_base: str,
        token: str,
        channel: str,
        user_name: str,
        *,
        last_active_path: Path = DEFAULT_LAST_ACTIVE_PATH,
        alerts: Any = None,
    ) -> None:
        self.channel = channel
        self._api_base = api_base.rstrip("/")
        self._token = token
        self._user_name = user_name or _DEFAULT_USER_NAME
        self._last_active_path = Path(last_active_path)
        self._alerts = alerts
        self._fail_count = 0

    def should_poll(self) -> bool:
        """last_active.read() → active channel.
        active == self.channel → True
        active == 'cli' or unreadable → 'tg' fallback takes it (tg is the
        main bridge).
        else → False (leave it for the active bridge to consume)."""
        la = last_active.read(self._last_active_path)
        active = la.get("channel") if la else None
        if active == self.channel:
            return True
        if active == "cli" or la is None:
            return self.channel == "tg"
        return False

    def fetch(self) -> list[str]:
        """POST {api_base}/signal/consume (Bearer token); render each pending
        signal to injection text. Network errors → return [] silently
        (10 consecutive failures → one alert)."""
        try:
            result = self._http("/signal/consume")
        except Exception as e:
            self._fail_count += 1
            logger.warning("qidu signal fetch failed (%d consecutive): %s", self._fail_count, e)
            if self._fail_count >= _MAX_CONSECUTIVE_FAILURES:
                if self._alerts:
                    self._alerts.write(
                        "warn", "qidu_signal_fetch_failed",
                        f"{self._fail_count} consecutive signal fetch failures: {e}",
                        source="qidu_signal",
                    )
                self._fail_count = 0
            return []
        self._fail_count = 0
        rendered = []
        for signal in result.get("signals", []):
            text = render_signal(
                signal.get("event_type", ""), signal.get("payload", {}), self._user_name
            )
            if text:
                rendered.append(text)
        return rendered

    def _http(self, path: str) -> dict:
        url = f"{self._api_base}{path}"
        req = urllib.request.Request(
            url,
            method="POST",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
