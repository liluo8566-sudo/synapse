"""Video keyframe extraction helper (C0).

cc Read tool cannot parse .mp4 binaries directly, so we pre-extract a
handful of JPEG keyframes alongside the WeChat-provided thumb. The
bridge then injects all of them via the Read-tool instruction line and
cc reasons over the still images.

Frame-count rule (by play_length seconds):
  - ≤ 5s  → 0 extra frames (thumb is enough)
  - ≤ 30s → 3 frames at 0.25 / 0.5 / 0.75 of play_length
  - > 30s → min(10, play_length // 6) evenly-spaced frames
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_FRAME_TIMEOUT_S = 30


def keyframe_timestamps(play_length: int) -> list[float]:
    """Pure timestamp computation — list of seconds offsets to extract.

    Empty list when no extras are needed (play_length ≤ 5).
    """
    if play_length <= 5:
        return []
    if play_length <= 30:
        return [round(play_length * f, 3) for f in (0.25, 0.5, 0.75)]
    n = min(10, play_length // 6)
    if n <= 0:
        return []
    # Evenly spaced inside (0, play_length), avoiding 0 and the very end.
    step = play_length / (n + 1)
    return [round(step * (i + 1), 3) for i in range(n)]


def ffmpeg_keyframes(
    mp4_path: Path,
    out_dir: Path,
    stem: str,
    play_length: int,
) -> list[Path]:
    """Extract keyframes from `mp4_path` into `out_dir/<stem>_kf<NN>.jpg`.

    Returns the list of successfully-written Paths in time order. On
    missing ffmpeg, missing source, or any subprocess failure we log a
    warning and return whatever frames we managed to write so the caller
    can still ship the mp4 + thumb.
    """
    if not mp4_path.exists():
        return []
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not on PATH — skipping keyframe extraction")
        return []

    timestamps = keyframe_timestamps(play_length)
    if not timestamps:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for idx, ts in enumerate(timestamps, start=1):
        out_path = out_dir / f"{stem}_kf{idx:02d}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(ts),
                    "-i",
                    str(mp4_path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(out_path),
                ],
                check=True,
                timeout=_FRAME_TIMEOUT_S,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning("ffmpeg keyframe @%.3fs failed: %s", ts, e)
            continue
        if out_path.exists():
            written.append(out_path)
    return written
