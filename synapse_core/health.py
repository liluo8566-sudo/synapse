"""HealthGate: persist boot/shutdown timestamps so we can detect unclean restarts.

State file shape:
    {"last_boot_ts": float, "last_clean_shutdown_ts": float, "boot_count": int}

`should_announce_restart()` is True iff the previous boot existed AND that boot
never recorded a clean shutdown — i.e. the bridge was killed by launchd respawn,
OS crash, panic, etc. First boot ever is always False.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class HealthGate:
    """Tracks bridge boot count and surfaces a 'restarted' signal once per restart."""

    def __init__(
        self,
        *,
        state_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_path = Path(state_path)
        self._clock = clock
        self._prev: dict[str, Any] = {}
        self._booted = False

    def boot(self) -> dict:
        """Record this boot. Returns previous boot info (or {} if first boot ever)."""
        prev = self._load()
        self._prev = dict(prev)
        new_state: dict[str, Any] = {
            "last_boot_ts": self._clock(),
            "last_clean_shutdown_ts": 0.0,
            "boot_count": int(prev.get("boot_count", 0)) + 1,
        }
        self._save(new_state)
        self._booted = True
        return prev

    def should_announce_restart(self) -> bool:
        """True iff a previous boot existed and never stamped a clean shutdown."""
        if not self._booted:
            return False
        if not self._prev:
            return False
        last_boot = self._prev.get("last_boot_ts", 0)
        last_clean = self._prev.get("last_clean_shutdown_ts", 0)
        try:
            return float(last_boot) > 0 and float(last_clean) < float(last_boot)
        except (TypeError, ValueError):
            return False

    def stamp_clean_shutdown(self) -> None:
        """Record a clean shutdown timestamp on top of the current state."""
        state = self._load()
        if not state:
            # nothing to stamp against; create a minimal record so a future boot
            # can tell this one came down cleanly.
            state = {
                "last_boot_ts": self._clock(),
                "last_clean_shutdown_ts": 0.0,
                "boot_count": 1,
            }
        state["last_clean_shutdown_ts"] = self._clock()
        self._save(state)

    # ── persistence ────────────────────────────────────────────────

    def _load(self) -> dict:
        if not self._state_path.is_file():
            return {}
        try:
            return json.loads(self._state_path.read_text() or "{}")
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("health state load failed: %s", e)
            return {}

    def _save(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self._state_path)
        except OSError as e:
            logger.warning("health state save failed: %s", e)
