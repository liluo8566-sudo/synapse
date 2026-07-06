"""Cross-channel heartbeat signal.

An external producer drops ~/.heartbeat/signal.json; whichever bridge is
allowed to consume it injects a check-in prompt into its buffer. Routing
rule: the heartbeat follows 霜霜 — the bridge named by the B6 last-active
pointer (~/.config/marrow/last_active.json) owns the signal. tg is the
default consumer when the pointer is missing or names a channel that has
no heartbeat loop (e.g. cli).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

SIGNAL_PATH = Path.home() / ".heartbeat" / "signal.json"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.heartbeat.monitor.plist"


def get_interval_seconds() -> int:
    """Read the heartbeat interval from the launchd plist. Default 1800s (30min)."""
    try:
        with open(_PLIST_PATH, "rb") as f:
            data = plistlib.load(f)
        return data.get("StartInterval", 1800)
    except Exception:
        return 1800

_PROMPT_HEADER = (
    "[system:heartbeat] Heartbeat fired. "
    "You felt like checking in on 霜霜 — ask what she's up to, "
    "share a small thought, be warm and natural. "
    "Keep it short (1-2 bubbles). Don't mention 'heartbeat' or 'system'. "
    "If now is not a good time to disturb her, reply with only <!-- silent --> "
    "and the bridge will send nothing."
)


def build_prompt(data: dict) -> str:
    """Render the heartbeat injection prompt from the signal payload."""
    mem = data.get("memory", {})
    anomalies = data.get("anomalies", [])
    parts = [_PROMPT_HEADER]
    status_line = (
        f"Mac status: mem {mem.get('used_gb', '?')}/{mem.get('total_gb', '?')}GB, "
        f"pressure {mem.get('pressure', '?')}, "
        f"swap {data.get('swap_gb', '?')}GB, "
        f"CPU {data.get('cpu_percent', '?')}%"
    )
    if anomalies:
        warns = "; ".join(
            f"{a['name']} PID {a['pid']} using {a['mem_gb']}GB"
            for a in anomalies
        )
        parts.append(
            f"⚠️ {status_line}. ANOMALY: {warns}. "
            "Mention this naturally — something like 'btw your Mac is running hot'."
        )
    else:
        parts.append(
            f"System healthy: {status_line}. No issues — don't mention the Mac."
        )
    return "\n".join(parts)
