"""C0 inbound media tests.

Covers: AES-128-ECB decrypt helper (symmetric to C1 encrypt), download +
decrypt round-trip, materialize() dispatcher routing per media type, and
the prompt-injection line builder.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from synapse_wx.ilink._media import (
    decrypt_aes_ecb,
    download_and_decrypt,
    encrypt_aes_ecb,
)
from synapse_wx.media.inbound import build_read_tool_instruction, materialize

# ── decrypt helper (symmetric to encrypt) ──────────────────────────────────


def test_decrypt_aes_ecb_roundtrip() -> None:
    """encrypt → decrypt yields original plaintext (PKCS7 stripped)."""
    key = b"k" * 16
    plain = b"hello world this is more than one block long!!"
    ct = encrypt_aes_ecb(plain, key)
    out = decrypt_aes_ecb(ct, key)
    assert out == plain


def test_decrypt_aes_ecb_rejects_bad_key() -> None:
    with pytest.raises(ValueError):
        decrypt_aes_ecb(b"x" * 32, b"short")


def test_decrypt_aes_ecb_non_block_aligned_returns_none() -> None:
    """Non-16-byte-multiple ciphertext is unsafe; helper returns None."""
    assert decrypt_aes_ecb(b"only13chars!!", b"k" * 16) is None


def test_decrypt_aes_ecb_strips_pkcs7_padding() -> None:
    """Final block padding is always stripped, even on exact-block boundary."""
    key = b"K" * 16
    plain = b"a" * 16  # exact block — PKCS7 still adds 16 bytes of padding
    ct = encrypt_aes_ecb(plain, key)
    assert decrypt_aes_ecb(ct, key) == plain


# ── download_and_decrypt (HTTP mocked) ─────────────────────────────────────


def _resp(status: int = 200, content: bytes = b"", body: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.content = content
    r.text = json.dumps(body) if body is not None else ""
    r.json.return_value = body or {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


def test_download_and_decrypt_roundtrips_aes(tmp_path: Path) -> None:
    """Server returns ciphertext; helper decrypts → writes plaintext to save_path."""
    key = b"K" * 16
    plain = b"\x89PNGfakeimagebytesfortest"
    ct = encrypt_aes_ecb(plain, key)
    import base64

    # _parse_aes_key tries base64(hex) first; produce that shape.
    aes_key_b64 = base64.b64encode(key.hex().encode("ascii")).decode("ascii")

    http = MagicMock(spec=httpx.Client)
    http.get.return_value = _resp(200, content=ct)
    out = tmp_path / "decrypted.png"
    ok = download_and_decrypt(http, "", aes_key_b64, out, encrypt_query_param="qp")
    assert ok is True
    assert out.read_bytes() == plain


def test_download_and_decrypt_returns_false_on_empty_url(tmp_path: Path) -> None:
    http = MagicMock(spec=httpx.Client)
    ok = download_and_decrypt(http, "", "k", tmp_path / "x.bin", encrypt_query_param="")
    assert ok is False


def test_download_and_decrypt_writes_raw_when_key_invalid(tmp_path: Path) -> None:
    """Garbage key → fallback to raw bytes (matches existing behaviour)."""
    http = MagicMock(spec=httpx.Client)
    http.get.return_value = _resp(200, content=b"\x89PNGraw")
    out = tmp_path / "raw.bin"
    ok = download_and_decrypt(http, "", "", out, encrypt_query_param="qp")
    # empty aes_key → key_bytes None → fallback to raw.
    assert ok is True
    assert out.read_bytes() == b"\x89PNGraw"


# ── materialize() — dispatcher per type ────────────────────────────────────


class _FakeILink:
    """Lightweight stand-in for ILinkClient.download_media."""

    def __init__(self, payload: bytes = b"FAKEDATA") -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, str, str]] = []

    def download_media(
        self,
        cdn_url: str,
        aes_key: str,
        save_path: Path,
        encrypt_query_param: str = "",
    ) -> bool:
        self.calls.append((cdn_url, aes_key, str(save_path), encrypt_query_param))
        save_path.write_bytes(self.payload)
        return True


def test_materialize_image_writes_file_and_returns_path(tmp_path: Path) -> None:
    """image event → ilink downloads + decrypts → returns the local Path."""
    ilink = _FakeILink(b"\x89PNGdata")
    ev = {
        "type": "image",
        "cdn_url": "https://cdn/img",
        "aes_key": "K" * 22,
        "encrypt_query_param": "QP-IMG",
    }
    paths = materialize(ev, ilink, tmp_path)
    assert len(paths) == 1
    path = paths[0]
    assert path.exists()
    assert path.parent == tmp_path / "Images"
    assert path.read_bytes() == b"\x89PNGdata"
    # filename hint: image → .jpg suffix
    assert path.suffix in (".jpg", ".jpeg", ".png")
    # ilink got the right args
    assert len(ilink.calls) == 1
    assert ilink.calls[0][0] == "https://cdn/img"
    assert ilink.calls[0][3] == "QP-IMG"


def test_materialize_voice_uses_inline_text_not_download(tmp_path: Path) -> None:
    """voice event has built-in iLink transcription; no download — write txt sidecar."""
    ilink = _FakeILink()
    ev = {"type": "voice", "text": "hello transcribed by ilink"}
    paths = materialize(ev, ilink, tmp_path)
    assert len(paths) == 1
    path = paths[0]
    assert path.exists()
    assert path.parent == tmp_path / "Transcripts"
    assert path.read_text() == "hello transcribed by ilink"
    assert path.suffix == ".txt"
    # No download triggered.
    assert ilink.calls == []


def test_materialize_voice_empty_text_returns_none(tmp_path: Path) -> None:
    ilink = _FakeILink()
    ev = {"type": "voice", "text": ""}
    assert materialize(ev, ilink, tmp_path) == []


def test_materialize_file_small_pdf_returns_raw_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF ≤20 pages → keep raw .pdf path; cc Read multi-steps via pages=."""
    from synapse_wx.media import inbound as inbound_mod
    from synapse_wx.media import pdf as pdf_mod

    ilink = _FakeILink(b"%PDF-fake")

    # Guard: even if extract_text were called, we'd notice — it must NOT run.
    called = {"n": 0}

    def fake_extract(src: Path) -> Path | None:
        called["n"] += 1
        sidecar = src.with_suffix(".txt")
        sidecar.write_text("should not be used")
        return sidecar

    monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)
    # Force "no pre-extract" verdict deterministically (no pdfinfo dep in CI).
    monkeypatch.setattr(inbound_mod, "_pdf_needs_pre_extract", lambda p: False)
    ev = {
        "type": "file",
        "cdn_url": "https://cdn/f",
        "aes_key": "K" * 22,
        "encrypt_query_param": "QP-PDF",
        "filename": "report.pdf",
    }
    paths = materialize(ev, ilink, tmp_path)
    assert len(paths) == 1
    path = paths[0]
    assert path.parent == tmp_path / "Files"
    assert path.suffix == ".pdf"
    assert path.read_bytes() == b"%PDF-fake"
    assert called["n"] == 0


def test_materialize_file_large_pdf_uses_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF >20 pages → run extractor, return .txt sidecar."""
    from synapse_wx.media import inbound as inbound_mod
    from synapse_wx.media import pdf as pdf_mod

    ilink = _FakeILink(b"%PDF-fake")

    def fake_extract(src: Path) -> Path | None:
        sidecar = src.with_suffix(".txt")
        sidecar.write_text("pdf body text here")
        return sidecar

    monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)
    monkeypatch.setattr(inbound_mod, "_pdf_needs_pre_extract", lambda p: True)
    ev = {
        "type": "file",
        "cdn_url": "https://cdn/f",
        "aes_key": "K" * 22,
        "encrypt_query_param": "QP-PDF",
        "filename": "report.pdf",
    }
    paths = materialize(ev, ilink, tmp_path)
    assert len(paths) == 1
    path = paths[0]
    # PDF sidecar lives alongside source PDF in the file/ subdir.
    assert path.parent == tmp_path / "Files"
    assert path.suffix == ".txt"
    assert path.read_text() == "pdf body text here"


def test_materialize_file_large_pdf_extractor_failure_falls_back_to_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF >20 pages + extractor returns None → raw .pdf path (cc Read uses pages=)."""
    from synapse_wx.media import inbound as inbound_mod
    from synapse_wx.media import pdf as pdf_mod

    ilink = _FakeILink(b"%PDF-fake")
    monkeypatch.setattr(pdf_mod, "extract_text", lambda src: None)
    monkeypatch.setattr(inbound_mod, "_pdf_needs_pre_extract", lambda p: True)
    ev = {
        "type": "file",
        "cdn_url": "https://cdn/f",
        "aes_key": "K" * 22,
        "encrypt_query_param": "QP-PDF",
        "filename": "report.pdf",
    }
    paths = materialize(ev, ilink, tmp_path)
    assert len(paths) == 1
    path = paths[0]
    assert path.parent == tmp_path / "Files"
    assert path.suffix == ".pdf"
    assert path.read_bytes() == b"%PDF-fake"


def test_materialize_file_non_pdf_returns_raw_path(tmp_path: Path) -> None:
    """Non-PDF file → just download + decrypt, return raw path."""
    ilink = _FakeILink(b"any-binary")
    ev = {
        "type": "file",
        "cdn_url": "https://cdn/f",
        "aes_key": "K" * 22,
        "encrypt_query_param": "QP",
        "filename": "notes.txt",
    }
    paths = materialize(ev, ilink, tmp_path)
    assert len(paths) == 1
    path = paths[0]
    assert path.parent == tmp_path / "Files"
    assert path.read_bytes() == b"any-binary"
    # extension preserved from filename hint
    assert path.suffix == ".txt"


def test_materialize_unknown_type_returns_none(tmp_path: Path) -> None:
    ilink = _FakeILink()
    assert materialize({"type": "sticker"}, ilink, tmp_path) == []


def test_materialize_download_failure_returns_none(tmp_path: Path) -> None:
    class _Failing:
        def download_media(self, *a, **kw) -> bool:
            return False

    ev = {
        "type": "image",
        "cdn_url": "https://cdn/img",
        "aes_key": "K",
        "encrypt_query_param": "QP",
    }
    assert materialize(ev, _Failing(), tmp_path) == []


# ── prompt injection helper ────────────────────────────────────────────────


def test_build_read_tool_instruction_single_path(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    p.write_bytes(b"x")
    out = build_read_tool_instruction([p])
    # Matches expected verbatim pattern.
    assert "Use the Read tool to view" in out
    assert str(p) in out


def test_build_read_tool_instruction_multi_path(tmp_path: Path) -> None:
    paths = [tmp_path / "a.jpg", tmp_path / "b.pdf"]
    for p in paths:
        p.write_bytes(b"x")
    out = build_read_tool_instruction(paths)
    assert str(paths[0]) in out
    assert str(paths[1]) in out


def test_build_read_tool_instruction_empty_returns_empty() -> None:
    assert build_read_tool_instruction([]) == ""
