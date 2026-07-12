from __future__ import annotations


class ProviderError(Exception):
    """Base class for provider errors."""


class ProviderSpawnError(ProviderError):
    """Subprocess failed to start."""


class ProviderDeadError(ProviderError):
    """Subprocess died unexpectedly (stdin closed or stdout EOF mid-stream)."""


class ProviderStallError(ProviderDeadError):
    """Subprocess stayed alive but produced no stream event for idle_hard_s.

    Subclasses ProviderDeadError so existing death-handling paths (respawn,
    alert) treat a stall exactly like a death without any new except clause.
    """
