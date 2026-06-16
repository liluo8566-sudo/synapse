"""C0 inbound video tests.

Covers the full video pipeline:
  - extract_media surfaces all locked schema fields + nested thumb triplet
  - materialize fans out into mp4 + thumb + 0..N ffmpeg keyframes
  - ffmpeg_keyframes computes the right number of frames per play_length
  - ffmpeg_keyframes degrades gracefully when ffmpeg is missing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_wx.ilink.client import ILinkClient
from synapse_wx.media import inbound as inbound_mod
from synapse_wx.media import video as video_mod
from synapse_wx.media.inbound import materialize
from synapse_wx.media.video import ffmpeg_keyframes, keyframe_timestamps

# ── extract_media schema lock ──────────────────────────────────────────────


def test_extract_media_video_full_shape() -> None:
    """A type=5 video item is decoded with all 6 top-level keys + nested thumb."""
    msg = {
        "item_list": [
            {
                "type": 5,
                "video_item": {
                    "play_length": 17,
                    "video_size": 1234567,
                    "video_md5": "abc123",
                    "media": {
                        "full_url": "https://cdn/video.mp4",
                        "aes_key": "AESKEYVIDEO==",
                        "encrypt_query_param": "QP-VIDEO",
                    },
                    "thumb_media": {
                        "full_url": "https://cdn/thumb.jpg",
                        "aes_key": "AESKEYTHUMB==",
                        "encrypt_query_param": "QP-THUMB",
                    },
                },
            }
        ]
    }
    out = ILinkClient.extract_media(msg)
    assert len(out) == 1
    ev = out[0]
    assert ev["type"] == "video"
    assert ev["cdn_url"] == "https://cdn/video.mp4"
    assert ev["aes_key"] == "AESKEYVIDEO=="
    assert ev["encrypt_query_param"] == "QP-VIDEO"
    assert ev["play_length"] == 17
    thumb = ev["thumb"]
    assert thumb["cdn_url"] == "https://cdn/thumb.jpg"
    assert thumb["aes_key"] == "AESKEYTHUMB=="
    assert thumb["encrypt_query_param"] == "QP-THUMB"


# ── materialize fan-out ────────────────────────────────────────────────────


class _FakeILink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def download_media(
        self,
        cdn_url: str,
        aes_key: str,
        save_path: Path,
        encrypt_query_param: str = "",
    ) -> bool:
        self.calls.append((cdn_url, aes_key, str(save_path), encrypt_query_param))
        save_path.write_bytes(b"FAKE")
        return True


def _video_event(play_length: int) -> dict:
    return {
        "type": "video",
        "cdn_url": "https://cdn/v.mp4",
        "aes_key": "K" * 22,
        "encrypt_query_param": "QP-V",
        "play_length": play_length,
        "thumb": {
            "cdn_url": "https://cdn/t.jpg",
            "aes_key": "T" * 22,
            "encrypt_query_param": "QP-T",
        },
    }


def test_materialize_video_thumb_only_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """play_length=3 → no keyframes; just mp4 + thumb."""
    monkeypatch.setattr(video_mod, "ffmpeg_keyframes", lambda *a, **kw: [])
    # Ensure materialize's local import picks up the monkeypatched module.
    monkeypatch.setattr(
        inbound_mod, "_materialize_video", inbound_mod._materialize_video
    )

    ilink = _FakeILink()
    paths = materialize(_video_event(3), ilink, tmp_path)
    assert len(paths) == 2
    mp4_path, thumb_path = paths
    assert mp4_path.parent == tmp_path / "Videos"
    assert mp4_path.suffix == ".mp4"
    assert thumb_path.parent == tmp_path / "Images"
    assert thumb_path.suffix == ".jpg"
    assert thumb_path.name.endswith("_thumb.jpg")
    # mp4 + thumb share the timestamp stem (sans `_thumb`).
    assert thumb_path.stem.removesuffix("_thumb") == mp4_path.stem


def test_materialize_video_with_keyframes_medium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """play_length=20 → mp4 + thumb + 3 keyframes."""
    image_dir = tmp_path / "Images"

    def fake_kf(
        mp4_path: Path, out_dir: Path, stem: str, play_length: int
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(1, 4):
            p = out_dir / f"{stem}_kf{i:02d}.jpg"
            p.write_bytes(b"FAKEKF")
            out.append(p)
        return out

    monkeypatch.setattr(video_mod, "ffmpeg_keyframes", fake_kf)

    ilink = _FakeILink()
    paths = materialize(_video_event(20), ilink, tmp_path)
    assert len(paths) == 5
    mp4_path, thumb_path, *kfs = paths
    assert mp4_path.parent == tmp_path / "Videos"
    assert thumb_path.parent == image_dir
    for kf in kfs:
        assert kf.parent == image_dir
        assert kf.suffix == ".jpg"
        assert "_kf" in kf.name


# ── ffmpeg_keyframes helper ────────────────────────────────────────────────


def test_ffmpeg_keyframes_missing_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ffmpeg on PATH → graceful empty list, no raise."""
    monkeypatch.setattr(video_mod.shutil, "which", lambda _name: None)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"FAKE")
    out_dir = tmp_path / "Images"
    assert ffmpeg_keyframes(src, out_dir, "stamp", 20) == []


def test_ffmpeg_keyframes_frame_count_rule() -> None:
    """Frame-count rule: 0/0/3/3/10/10 for 1/5/6/30/60/120."""
    expected = {
        1: 0,
        5: 0,
        6: 3,
        30: 3,
        60: 10,
        120: 10,
    }
    for play_length, n in expected.items():
        assert len(keyframe_timestamps(play_length)) == n, (
            f"play_length={play_length} expected {n} timestamps"
        )
