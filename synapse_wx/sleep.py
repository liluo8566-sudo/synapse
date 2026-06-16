"""SleepWakeObserver: bind macOS NSWorkspace sleep/wake notifications to handlers.

pyobjc is lazy-imported inside `start()` so non-mac CI / pytest collection
still works without the framework installed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from synapse_core.alerts import AlertSink

logger = logging.getLogger(__name__)

_SLEEP_NAME = "NSWorkspaceWillSleepNotification"
_WAKE_NAME = "NSWorkspaceDidWakeNotification"


class SleepWakeObserver:
    """Observe NSWorkspace will-sleep / did-wake; dispatch into Python handlers."""

    def __init__(
        self,
        *,
        will_sleep: Callable[[], None],
        did_wake: Callable[[], None],
        alerts: AlertSink | None = None,
    ) -> None:
        self._will_sleep = will_sleep
        self._did_wake = did_wake
        self._alerts = alerts
        self._observer: object | None = None
        self._notification_center: object | None = None

    def start(self) -> None:
        """Register the observer. No-op + warn alert if pyobjc unavailable."""
        if self._observer is not None:
            return
        try:
            import Cocoa  # noqa: F401 - pyobjc binding
            import objc
            from Foundation import NSObject
        except ImportError as e:
            logger.warning("pyobjc unavailable; sleep-detect disabled (%s)", e)
            if self._alerts is not None:
                self._alerts.write(
                    "warn",
                    "pyobjc_missing",
                    f"pyobjc unavailable; sleep-detect disabled ({e})",
                    source="sleep.SleepWakeObserver.start",
                )
            return

        will_sleep_cb = self._will_sleep
        did_wake_cb = self._did_wake

        class _SleepWakeBridge(NSObject):
            def willSleep_(self, _notification):  # noqa: N802 - ObjC selector
                try:
                    will_sleep_cb()
                except Exception:
                    logger.exception("will_sleep handler raised")

            def didWake_(self, _notification):  # noqa: N802 - ObjC selector
                try:
                    did_wake_cb()
                except Exception:
                    logger.exception("did_wake handler raised")

            # Help pyobjc see these as exposed selectors.
            willSleep_ = objc.python_method(willSleep_)  # type: ignore[assignment]
            didWake_ = objc.python_method(didWake_)  # type: ignore[assignment]

        observer = _SleepWakeBridge.alloc().init()
        center = Cocoa.NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer, "willSleep:", _SLEEP_NAME, None
        )
        center.addObserver_selector_name_object_(
            observer, "didWake:", _WAKE_NAME, None
        )
        self._observer = observer
        self._notification_center = center

    def stop(self) -> None:
        """Detach the observer if it was registered."""
        if self._observer is None or self._notification_center is None:
            return
        try:
            self._notification_center.removeObserver_(self._observer)
        except Exception as e:
            logger.warning("sleep observer removeObserver_ failed: %s", e)
        self._observer = None
        self._notification_center = None
