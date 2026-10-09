"""Shared logging setup for Synapse bridges."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
BACKUP_COUNT = 14

# apscheduler logs this exact warning (via its "apscheduler.scheduler" logger)
# whenever a job is still running when its next tick comes due — e.g.
# check_flush at interval=0.5s with max_instances=1 during a flush in
# progress. Benign and, at that frequency, the dominant line in the bridge
# log (110,938 of the last 200,000 measured in production). Every other
# apscheduler warning must still log.
_APSCHEDULER_MAX_INSTANCES_NEEDLE = "skipped: maximum number of running instances reached"


class _ApschedulerMaxInstancesFilter(logging.Filter):
    """Drops only that one warning, from any apscheduler.* logger.

    A logging.Filter attached to a PARENT logger (e.g. "apscheduler") is
    never consulted for records logged through a CHILD logger (e.g.
    "apscheduler.scheduler", the one that actually emits this warning) —
    Logger.handle() only checks the originating logger's own filters.
    Attached to the handlers instead, this sees every record that reaches
    them regardless of which apscheduler sub-logger it came from; the name
    check below keeps every other logger untouched."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("apscheduler"):
            return True
        return _APSCHEDULER_MAX_INSTANCES_NEEDLE not in record.getMessage()


def configure_logging(log_path: Path) -> None:
    level = getattr(
        logging,
        os.environ.get("SYNAPSE_LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    )
    log_path = log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        if getattr(handler, "_synapse_configured", False):
            root.removeHandler(handler)
            handler.close()

    noise_filter = _ApschedulerMaxInstancesFilter()

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler.addFilter(noise_filter)
    file_handler._synapse_configured = True  # type: ignore[attr-defined]

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    stream_handler.addFilter(noise_filter)
    stream_handler._synapse_configured = True  # type: ignore[attr-defined]

    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
