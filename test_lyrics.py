"""Tests for synced-lyrics-linux (lyrics.py only -- no Qt needed).

Run:  python3 test_lyrics.py
  or: python3 -m pytest test_lyrics.py -v        (pytest optional)

The live LRCLIB test is skipped gracefully when offline: it never fails
the suite for lack of network, it prints SKIP instead.
"""
from __future__ import annotations

import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lyrics import (  # noqa: E402
    LyricsResult,
    LyricLine,
    fetch_lrclib,
    parse_lrc,
)


def test_basic_parse():
    lines = parse_lrc("[00:05.00] Look at the stars\n[00:09.50] Look how they shine\n")
    assert len(lines) == 2, lines
    assert lines[0] == LyricLine(5.0, "Look at the stars")
    assert lines[1].time == 9.5


def test_mm_ss_without_fraction():
    lines = parse_lrc("[01:02] One minute two\n[02:00] Two minutes\n")
    assert [l.time for l in lines] == [62.0, 120.0]


def test_multiple_timestamps_per_line():
    lines = parse_lrc("[00:10.00][00:20.00] Chorus line\n")
    assert len(lines) == 2, lines
    assert lines[0].time == 10.0 and lines[1].time == 20.0
    assert lines[0].text == lines[1].text == "Chorus line"


def test_offset_tag():
    # [offset:+1000] shifts every line 1 s later; negative shifts earlier.
    lines = parse_lrc("[offset:+1000]\n[00:10.00] Shifted\n")
    assert lines[0].time == 11.0
    lines = parse_lrc("[offset:-500]\n[00:10.00] Shifted\n")
    assert lines[0].time == 9.5


def test_metadata_and_empty_lines_ignored():
    text = "[ti:Yellow]\n[ar:Coldplay]\n[00:05.00] Real line\n\n[00:09.00]   \n[00:12.00] Next\n"
    lines = parse_lrc(text)
    assert [l.text for l in lines] == ["Real line", "Next"]


def test_enhanced_word_tags_stripped():
    text = "[00:10.00] <00:10.00> Look <00:11.00> at <00:12.00> the stars\n"
    lines = parse_lrc(text)
    assert len(lines) == 1
    assert lines[0].text == "Look at the stars"
    assert lines[0].time == 10.0


def test_out_of_order_lines_sorted():
    lines = parse_lrc("[00:20.00] Second\n[00:10.00] First\n")
    assert [l.text for l in lines] == ["First", "Second"]


def test_line_at_with_offset():
    result = LyricsResult(artist="A", track="T", synced_lines=[
        LyricLine(0.0, "one"), LyricLine(10.0, "two"), LyricLine(20.0, "three")])
    assert result.line_at(0.0) == 0
    assert result.line_at(9.9) == 0
    assert result.line_at(10.0) == 1
    assert result.line_at(25.0) == 2
    assert result.line_at(10.0, offset=-0.5) == 0  # sync offset shifts lookup
    assert LyricsResult(artist="A", track="T").line_at(5.0) == -1


def test_live_lrclib_fetch():
    """Live check: Coldplay — Yellow (269 s) should have synced lyrics.
    Skipped (not failed) when there is no network."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_lrclib("Coldplay", "Yellow", "Parachutes", 269.0,
                                  cache_dir=Path(tmp))
    except (urllib.error.URLError, OSError) as e:
        print(f"  SKIP live LRCLIB test (offline: {e})")
        return "skip"
    assert result is not None, "LRCLIB returned no match for Coldplay — Yellow"
    assert result.has_synced, "LRCLIB match had no synced lyrics"
    assert len(result.synced_lines) >= 10, f"only {len(result.synced_lines)} lines"
    print(f"  live: {result.artist} — {result.track}, "
          f"{len(result.synced_lines)} synced lines, source={result.source}")
    return "pass"


TESTS = [
    test_basic_parse,
    test_mm_ss_without_fraction,
    test_multiple_timestamps_per_line,
    test_offset_tag,
    test_metadata_and_empty_lines_ignored,
    test_enhanced_word_tags_stripped,
    test_out_of_order_lines_sorted,
    test_line_at_with_offset,
    test_live_lrclib_fetch,
]


def main() -> int:
    passed = skipped = failed = 0
    for test in TESTS:
        try:
            outcome = test()
            if outcome == "skip":
                skipped += 1
                print(f"SKIP {test.__name__}")
            else:
                passed += 1
                print(f"PASS {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {test.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - report, don't hide
            failed += 1
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed "
          f"({len(TESTS)} tests)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
