"""Tests for synapse_wx.usage.UsageClient."""

from __future__ import annotations

import json
import logging

from synapse_core.usage import USAGE_URL, Usage, UsageClient

_SAMPLE_BODY = json.dumps(
    {
        "five_hour": {
            "utilization": 42.0,
            "resets_at": "2026-06-02T19:00:00+00:00",
        },
        "seven_day": {
            "utilization": 17.0,
            "resets_at": "2026-06-04T17:00:00+00:00",
        },
        "extra_usage": None,
    }
).encode()


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Http:
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append((url, headers))
        if not self._responses:
            raise AssertionError("unexpected extra HTTP call")
        return self._responses.pop(0)


# ── happy path ─────────────────────────────────────────────────────


def test_fetch_parses_200_and_caches() -> None:
    clock = _Clock()
    http = _Http([(200, _SAMPLE_BODY)])
    c = UsageClient(
        clock=clock,
        ttl_sec=300,
        http_get=http,
        token_loader=lambda: "tok",
    )
    usage = c.fetch()
    assert isinstance(usage, Usage)
    assert usage.five_hour_pct == 42.0
    assert usage.seven_day_pct == 17.0
    assert usage.five_hour_resets_at_unix is not None
    assert len(http.calls) == 1
    url, headers = http.calls[0]
    assert url == USAGE_URL
    assert headers["Authorization"] == "Bearer tok"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


def test_cache_hit_within_ttl_skips_http() -> None:
    clock = _Clock()
    http = _Http([(200, _SAMPLE_BODY)])
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: "tok"
    )
    first = c.fetch()
    clock.t += 100  # < ttl
    second = c.fetch()
    assert first is second
    assert len(http.calls) == 1  # no refresh


def test_cache_expired_refetches() -> None:
    clock = _Clock()
    second_body = _SAMPLE_BODY.replace(b"42.0", b"55.0")
    http = _Http([(200, _SAMPLE_BODY), (200, second_body)])
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: "tok"
    )
    first = c.fetch()
    clock.t += 400  # > ttl
    second = c.fetch()
    assert first is not None and second is not None
    assert first.five_hour_pct == 42.0
    assert second.five_hour_pct == 55.0
    assert len(http.calls) == 2


# ── failure modes ─────────────────────────────────────────────────


def test_429_returns_stale_cache(caplog) -> None:
    clock = _Clock()
    http = _Http([(200, _SAMPLE_BODY), (429, b"{}")])
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: "tok"
    )
    good = c.fetch()
    clock.t += 400
    with caplog.at_level(logging.WARNING, logger="synapse_core.usage"):
        stale = c.fetch()
    assert stale is good  # served from cache
    assert any("http 429" in r.message.lower() for r in caplog.records)


def test_429_with_no_cache_returns_none(caplog) -> None:
    clock = _Clock()
    http = _Http([(429, b"{}")])
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: "tok"
    )
    with caplog.at_level(logging.WARNING, logger="synapse_core.usage"):
        assert c.fetch() is None
    assert any("http 429" in r.message.lower() for r in caplog.records)


def test_network_error_returns_none(caplog) -> None:
    clock = _Clock()

    def boom(_url: str, _headers: dict[str, str]) -> tuple[int, bytes]:
        raise OSError("connection refused")

    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=boom, token_loader=lambda: "tok"
    )
    with caplog.at_level(logging.WARNING, logger="synapse_core.usage"):
        assert c.fetch() is None
    assert any("http error" in r.message.lower() for r in caplog.records)


def test_missing_token_returns_none_with_warning(caplog) -> None:
    clock = _Clock()
    http = _Http([])  # never reached
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: None
    )
    with caplog.at_level(logging.WARNING, logger="synapse_core.usage"):
        assert c.fetch() is None
    assert any("no oauth token" in r.message.lower() for r in caplog.records)
    assert http.calls == []


def test_bad_json_returns_stale(caplog) -> None:
    clock = _Clock()
    http = _Http([(200, _SAMPLE_BODY), (200, b"not json")])
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: "tok"
    )
    good = c.fetch()
    clock.t += 400
    with caplog.at_level(logging.WARNING, logger="synapse_core.usage"):
        result = c.fetch()
    assert result is good
    assert any("bad json" in r.message.lower() for r in caplog.records)


def test_token_loader_raises_returns_none(caplog) -> None:
    clock = _Clock()
    http = _Http([])

    def boom() -> str | None:
        raise RuntimeError("keychain locked")

    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=boom
    )
    with caplog.at_level(logging.WARNING, logger="synapse_core.usage"):
        assert c.fetch() is None
    assert any("token load failed" in r.message.lower() for r in caplog.records)


def test_partial_response_one_window_null() -> None:
    clock = _Clock()
    body = json.dumps(
        {
            "five_hour": {"utilization": 12.0, "resets_at": "2026-06-02T19:00:00+00:00"},
            "seven_day": None,
        }
    ).encode()
    http = _Http([(200, body)])
    c = UsageClient(
        clock=clock, ttl_sec=300, http_get=http, token_loader=lambda: "tok"
    )
    usage = c.fetch()
    assert usage is not None
    assert usage.five_hour_pct == 12.0
    assert usage.seven_day_pct is None
    assert usage.seven_day_resets_at_unix is None
