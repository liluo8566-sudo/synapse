"""Image downscale helper for outbound sends (C1).

Uses macOS `sips` to produce a JPEG copy sized for the WeChat CDN ceiling
(~512KB ciphertext after AES-ECB padding). Never crashes — on any sips
failure it logs a warning and returns the original path unchanged.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_SIPS = "/usr/bin/sips"
_SIPS_TIMEOUT_S = 30

# Ladder: (max_dim, quality) — tried in order until output <= max_bytes.
_LADDER = [
    (1280, 80),
    (1024, 70),
    (800, 60),
]


def downscale_for_send(
    path: Path,
    *,
    max_bytes: int = 250_000,
    max_dim: int = 1280,
) -> Path:
    """Return a send-ready image path, downscaling if necessary.

    If `path` is already <= max_bytes, returns it unchanged (no transcode).
    Otherwise shells out to sips to produce a JPEG copy in a temp dir,
    stepping through the quality/dimension ladder until <= max_bytes.
    On any failure, logs a warning and returns the original path (best-effort).

    The caller is responsible for cleaning up any temp file returned.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        logger.warning("downscale_for_send: missing file %s", p)
        return p

    if p.stat().st_size <= max_bytes:
        return p

    tmp_dir = Path(tempfile.mkdtemp(prefix="synapse_wx_img_"))

    for dim, quality in _LADDER:
        # sips -Z <dim> -s format jpeg -s formatOptions <quality> <in> --out <out>
        out_path = tmp_dir / (p.stem + ".jpg")
        try:
            result = subprocess.run(
                [
                    _SIPS,
                    "-Z", str(dim),
                    "-s", "format", "jpeg",
                    "-s", "formatOptions", str(quality),
                    str(p),
                    "--out", str(out_path),
                ],
                check=True,
                timeout=_SIPS_TIMEOUT_S,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning("sips downscale failed (dim=%d q=%d): %s", dim, quality, e)
            _cleanup(tmp_dir)
            return p

        if not out_path.exists():
            logger.warning("sips produced no output file (dim=%d q=%d)", dim, quality)
            _cleanup(tmp_dir)
            return p

        if out_path.stat().st_size <= max_bytes:
            return out_path

    # All ladder steps exhausted — return best attempt (smallest = last step).
    out_path = tmp_dir / (p.stem + ".jpg")
    if out_path.exists():
        logger.warning(
            "downscale_for_send: could not reduce %s below %d bytes; returning best attempt",
            p.name,
            max_bytes,
        )
        return out_path

    _cleanup(tmp_dir)
    return p


def _cleanup(tmp_dir: Path) -> None:
    """Remove temp dir created by downscale_for_send on failure paths."""
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass
