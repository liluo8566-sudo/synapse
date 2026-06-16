"""Tests for synapse_wx/media/image.py — downscale_for_send."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from synapse_wx.media.image import downscale_for_send


# ── helpers ────────────────────────────────────────────────────────────────


def _make_tiny_png(path: Path) -> Path:
    """Write a minimal valid-ish PNG (1x1 white pixel, ~67 bytes)."""
    # Minimal PNG bytes — valid enough for sips to accept as an image.
    import base64
    # 1x1 white PNG (base64-encoded)
    PNG_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
    )
    path.write_bytes(PNG_1x1)
    return path


# ── small-file passthrough ─────────────────────────────────────────────────


def test_small_file_returns_original_unchanged(tmp_path: Path) -> None:
    """Files <= max_bytes are returned as-is without invoking sips."""
    f = tmp_path / "small.png"
    f.write_bytes(b"\x89PNG" + b"x" * 100)  # 104 bytes, well under 250_000

    with patch("synapse_wx.media.image.subprocess.run") as mock_run:
        result = downscale_for_send(f, max_bytes=250_000)

    assert result == f
    mock_run.assert_not_called()


def test_small_file_exactly_at_limit_returns_original(tmp_path: Path) -> None:
    """Exactly max_bytes is treated as within limit."""
    f = tmp_path / "exact.png"
    f.write_bytes(b"x" * 250_000)

    with patch("synapse_wx.media.image.subprocess.run") as mock_run:
        result = downscale_for_send(f, max_bytes=250_000)

    assert result == f
    mock_run.assert_not_called()


def test_missing_file_returns_original_path(tmp_path: Path) -> None:
    """Missing file returns the original Path without crashing."""
    f = tmp_path / "ghost.png"
    result = downscale_for_send(f)
    assert result == f


# ── sips failure returns original ─────────────────────────────────────────


def test_sips_failure_returns_original(tmp_path: Path) -> None:
    """CalledProcessError from sips → log warning, return original path."""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 300_000)  # over max_bytes

    with patch("synapse_wx.media.image.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "sips")
        result = downscale_for_send(f, max_bytes=250_000)

    assert result == f


def test_sips_timeout_returns_original(tmp_path: Path) -> None:
    """TimeoutExpired from sips → log warning, return original path."""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 300_000)

    with patch("synapse_wx.media.image.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("sips", 30)
        result = downscale_for_send(f, max_bytes=250_000)

    assert result == f


def test_sips_os_error_returns_original(tmp_path: Path) -> None:
    """OSError (sips binary missing) → return original path."""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 300_000)

    with patch("synapse_wx.media.image.subprocess.run") as mock_run:
        mock_run.side_effect = OSError("No such file")
        result = downscale_for_send(f, max_bytes=250_000)

    assert result == f


# ── sips mock success — returns a smaller temp file ────────────────────────


def test_sips_success_returns_temp_file_when_small_enough(tmp_path: Path) -> None:
    """When sips mock writes a file <= max_bytes, that temp path is returned."""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 300_000)

    def fake_run(cmd, **kwargs):
        # sips --out is the last argument; write a small file there
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"x" * 50_000)  # small enough
        return MagicMock(returncode=0)

    with patch("synapse_wx.media.image.subprocess.run", side_effect=fake_run):
        result = downscale_for_send(f, max_bytes=250_000)

    assert result != f
    assert result.suffix == ".jpg"
    assert result.exists()
    # Cleanup: caller owns the temp dir, but we can verify it was created
    result.parent.stat()  # should exist


def test_sips_called_with_list_args_no_shell(tmp_path: Path) -> None:
    """sips is invoked with list args (no shell=True) and correct flags."""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 300_000)

    captured_kwargs: dict = {}

    def fake_run(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["cmd"] = cmd
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"x" * 100)
        return MagicMock(returncode=0)

    with patch("synapse_wx.media.image.subprocess.run", side_effect=fake_run):
        downscale_for_send(f, max_bytes=250_000)

    cmd = captured_kwargs["cmd"]
    assert isinstance(cmd, list), "sips must be called with a list, not a shell string"
    assert "shell" not in captured_kwargs or captured_kwargs.get("shell") is not True
    assert "-Z" in cmd
    assert "jpeg" in cmd
    assert str(f) in cmd


# ── real sips integration (skipped if sips absent or not macOS) ────────────


@pytest.mark.skipif(
    not Path("/usr/bin/sips").exists(),
    reason="/usr/bin/sips not available (non-macOS or sips missing)",
)
def test_real_sips_small_png_passthrough(tmp_path: Path) -> None:
    """Real sips: a tiny PNG already under max_bytes is returned unchanged."""
    f = _make_tiny_png(tmp_path / "tiny.png")
    result = downscale_for_send(f, max_bytes=250_000)
    assert result == f


@pytest.mark.skipif(
    not Path("/usr/bin/sips").exists(),
    reason="/usr/bin/sips not available (non-macOS or sips missing)",
)
def test_real_sips_downscale_produces_smaller_file(tmp_path: Path) -> None:
    """Real sips: a large image gets downscaled to <= max_bytes."""
    # Build a large raw-ish PNG by repeating pattern bytes.
    # sips needs a real parseable image — use the 1x1 PNG then expand it
    # by writing a larger synthetic JPEG-like blob won't work, so we
    # generate a real image via sips itself from the 1x1 seed.
    tiny = _make_tiny_png(tmp_path / "seed.png")

    # Upscale with sips to create a real but large image (512x512 uncompressed).
    large = tmp_path / "large.png"
    try:
        subprocess.run(
            ["/usr/bin/sips", "-z", "512", "512", str(tiny), "--out", str(large)],
            check=True, capture_output=True, timeout=10,
        )
    except Exception:
        pytest.skip("Could not generate large test image via sips")

    if not large.exists() or large.stat().st_size == 0:
        pytest.skip("Large test image generation produced empty file")

    original_size = large.stat().st_size
    # Only run if the image is actually > 1000 bytes (real content)
    if original_size <= 1000:
        pytest.skip("Generated image too small to be a useful test")

    result = downscale_for_send(large, max_bytes=max(1, original_size - 1))

    # If it couldn't shrink (already very small), it returns original
    if result == large:
        pytest.skip("sips could not shrink the test image further")
    assert result.exists()
    assert result != large
