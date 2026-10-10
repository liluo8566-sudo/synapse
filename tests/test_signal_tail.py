"""T11 signal ear: the pure file-tail helper."""

from __future__ import annotations

from synapse_tg.signal_tail import tail_new_lines


def test_missing_file_returns_no_lines_offset_unchanged(tmp_path):
    lines, offset = tail_new_lines(str(tmp_path / "nope.log"), 42)
    assert lines == []
    assert offset == 42


def test_fresh_file_from_zero_reads_every_complete_line(tmp_path):
    p = tmp_path / "s.log"
    p.write_text("a\nb\nc\n")
    lines, offset = tail_new_lines(str(p), 0)
    assert lines == ["a", "b", "c"]
    assert offset == p.stat().st_size


def test_partial_trailing_line_is_not_consumed(tmp_path):
    p = tmp_path / "s.log"
    p.write_text("a\nb\n")
    lines, offset = tail_new_lines(str(p), 0)
    assert lines == ["a", "b"]
    assert offset == len("a\nb\n")

    with p.open("a") as f:
        f.write("partial")
    lines2, offset2 = tail_new_lines(str(p), offset)
    assert lines2 == []
    assert offset2 == offset          # nothing consumed — no trailing \n yet

    with p.open("a") as f:
        f.write(" done\n")
    lines3, offset3 = tail_new_lines(str(p), offset2)
    assert lines3 == ["partial done"]
    assert offset3 == p.stat().st_size


def test_truncated_file_resets_to_zero(tmp_path):
    p = tmp_path / "s.log"
    p.write_text("a\nb\nc\n")
    big_offset = p.stat().st_size + 1000
    p.write_text("fresh\n")            # replaced with a shorter file
    lines, offset = tail_new_lines(str(p), big_offset)
    assert lines == ["fresh"]
    assert offset == p.stat().st_size


def test_blank_lines_are_dropped(tmp_path):
    p = tmp_path / "s.log"
    p.write_text("a\n\n   \nb\n")
    lines, _offset = tail_new_lines(str(p), 0)
    assert lines == ["a", "b"]


def test_no_new_bytes_returns_no_lines(tmp_path):
    p = tmp_path / "s.log"
    p.write_text("a\n")
    offset = p.stat().st_size
    lines, new_offset = tail_new_lines(str(p), offset)
    assert lines == []
    assert new_offset == offset


def test_invalid_utf8_is_replaced_not_raised(tmp_path):
    p = tmp_path / "s.log"
    p.write_bytes(b"ok\n\xff\xfe broken\n")
    lines, offset = tail_new_lines(str(p), 0)
    assert len(lines) == 2
    assert lines[0] == "ok"
    assert "broken" in lines[1]
    assert offset == p.stat().st_size
