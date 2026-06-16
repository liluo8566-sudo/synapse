"""C0 PDF extraction wrapper tests.

`extract_text(path)` tries `pdftotext` subprocess first, then `markitdown`,
then falls back to None (caller keeps the raw PDF path so cc Read can
still view it directly).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse_wx.media import pdf as pdf_mod


def _make_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fakeheader\n")
    return pdf


def test_extract_text_uses_pdftotext_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _make_pdf(tmp_path)

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/pdftotext" if cmd == "pdftotext" else None

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        # pdftotext writes to the sidecar argv[-1].
        sidecar = Path(cmd[-1])
        sidecar.write_text("extracted body text")
        return MagicMock(returncode=0)

    monkeypatch.setattr(pdf_mod.shutil, "which", fake_which)
    monkeypatch.setattr(pdf_mod.subprocess, "run", fake_run)

    out = pdf_mod.extract_text(pdf)
    assert out is not None
    assert out.suffix == ".txt"
    assert out.read_text() == "extracted body text"


def test_extract_text_falls_back_to_markitdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _make_pdf(tmp_path)
    seen: list[str] = []

    def fake_which(cmd: str) -> str | None:
        seen.append(cmd)
        return "/usr/local/bin/markitdown" if cmd == "markitdown" else None

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(returncode=0, stdout=b"# title\n\nbody markdown\n")

    monkeypatch.setattr(pdf_mod.shutil, "which", fake_which)
    monkeypatch.setattr(pdf_mod.subprocess, "run", fake_run)

    out = pdf_mod.extract_text(pdf)
    assert out is not None
    assert out.suffix == ".txt"
    assert "body markdown" in out.read_text()
    # Confirm pdftotext was probed first.
    assert seen[0] == "pdftotext"


def test_extract_text_returns_none_when_no_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No extractor on PATH → return None; caller uses raw PDF path."""
    pdf = _make_pdf(tmp_path)
    monkeypatch.setattr(pdf_mod.shutil, "which", lambda _cmd: None)
    assert pdf_mod.extract_text(pdf) is None


def test_extract_text_returns_none_on_subprocess_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _make_pdf(tmp_path)
    monkeypatch.setattr(
        pdf_mod.shutil, "which", lambda c: "/usr/bin/pdftotext" if c == "pdftotext" else None
    )

    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, "pdftotext")

    monkeypatch.setattr(pdf_mod.subprocess, "run", boom)
    assert pdf_mod.extract_text(pdf) is None


def test_extract_text_missing_file_returns_none(tmp_path: Path) -> None:
    assert pdf_mod.extract_text(tmp_path / "missing.pdf") is None
