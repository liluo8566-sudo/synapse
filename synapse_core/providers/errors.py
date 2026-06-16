from __future__ import annotations


class ProviderError(Exception):
    """Base class for provider errors."""


class ProviderSpawnError(ProviderError):
    """Subprocess failed to start."""


class ProviderDeadError(ProviderError):
    """Subprocess died unexpectedly (stdin closed or stdout EOF mid-stream)."""
