"""PDF text extraction wrapper for inbound media (C0).

Called only for PDFs over 20 pages — cc Read can multi-step smaller ones
natively via the `pages=` parameter. Pre-extraction is a token-saving
optimisation (one extract vs many 20-page reads), not a capability gate.

Try `pdftotext` (poppler) first; fall back to `markitdown`. On any
failure return None — the caller falls back to the raw PDF path (cc Read
with `pages=` param is still better than nothing).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_text(src: Path) -> Path | None:
    """Extract text from `src` PDF. Return Path to .txt sidecar or None.

    The sidecar is written next to `src` with the same stem and `.txt`
    suffix. Failures (no extractor, subprocess error, missing file) are
    swallowed — return None and the caller falls back to the raw PDF path.
    """
    if not src.exists() or not src.is_file():
        return None

    sidecar = src.with_suffix(".txt")

    # Probe pdftotext (poppler) first — fastest, ships on most macs via brew.
    if shutil.which("pdftotext"):
        try:
            subprocess.run(
                ["pdftotext", "-layout", str(src), str(sidecar)],
                check=True,
                timeout=30,
                capture_output=True,
            )
            if sidecar.exists():
                return sidecar
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning("pdftotext failed: %s", e)

    # Fallback markitdown (stdout-style, write to sidecar ourselves).
    if shutil.which("markitdown"):
        try:
            result = subprocess.run(
                ["markitdown", str(src)],
                check=True,
                timeout=60,
                capture_output=True,
            )
            text = result.stdout
            if text:
                if isinstance(text, bytes):
                    sidecar.write_bytes(text)
                else:
                    sidecar.write_text(text)
                return sidecar
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning("markitdown failed: %s", e)

    return None
