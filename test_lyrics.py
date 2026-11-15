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
    LyricWord,
    fetch_betterlyrics,
    fetch_lrclib,
    parse_lrc,
    parse_ttml,
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


def test_enhanced_word_tags_parsed():
    """Enhanced LRC: <mm:ss.xx>word tags become per-word timings
    (they used to be stripped — the line text is the same either way)."""
    text = "[00:10.00] <00:10.00> Look <00:11.00> at <00:12.00> the stars\n[00:15.00] Next line\n"
    lines = parse_lrc(text)
    assert len(lines) == 2
    assert lines[0].text == "Look at the stars"
    assert lines[0].time == 10.0
    assert [(w.time, w.end, w.text) for w in lines[0].words] == [
        (10.0, 11.0, "Look"), (11.0, 12.0, "at"), (12.0, 15.0, "the stars")], \
        lines[0].words
    assert lines[1].words == []  # plain line: line-only sync


def test_elrc_two_line_fixture():
    """The BetterLyrics export shape: every word timestamped."""
    text = (
        "[00:01.050]<00:01.050>Virtual <00:01.390>Insanity\n"
        "[00:22.139]<00:22.139>Oh <00:23.009>hey\n"
    )
    lines = parse_lrc(text)
    assert len(lines) == 2
    assert lines[0].text == "Virtual Insanity"
    assert [round(w.time, 3) for w in lines[0].words] == [1.05, 1.39]
    assert lines[0].words[-1].end == lines[1].time  # last word ends at next line
    assert lines[1].words[0].text == "Oh"


def test_word_at_and_has_word_sync():
    result = LyricsResult(artist="A", track="T", synced_lines=[
        LyricLine(0.0, "one two", [
            LyricWord(0.0, 1.0, "one"), LyricWord(1.0, 2.0, "two")]),
        LyricLine(10.0, "three")])
    assert result.has_word_sync
    assert result.word_at(0, 0.0) == 0
    assert result.word_at(0, 0.9) == 0
    assert result.word_at(0, 1.0) == 1
    assert result.word_at(0, 5.0) == 1
    assert result.word_at(1, 10.0) == -1  # no word timings on this line
    assert result.word_at(5, 0.0) == -1   # no such line
    assert result.word_at(0, 0.0, offset=-0.5) == -1  # offset shifts lookup
    assert not LyricsResult(artist="A", track="T",
                            synced_lines=[LyricLine(0.0, "plain")]).has_word_sync


def test_parse_ttml_docs_example():
    """The TTML example from the Better Lyrics API docs."""
    ttml = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
<p begin="00:00:18.234" end="00:00:21.891"><span begin="00:00:18.234" end="00:00:18.567">The </span><span begin="00:00:18.567" end="00:00:18.901">club </span><span begin="00:00:18.901" end="00:00:19.234">isn't </span><span begin="00:00:19.234" end="00:00:19.568">the </span><span begin="00:00:19.568" end="00:00:19.901">best </span><span begin="00:00:19.901" end="00:00:20.234">place </span><span begin="00:00:20.234" end="00:00:20.568">to </span><span begin="00:00:20.568" end="00:00:20.901">find </span><span begin="00:00:20.901" end="00:00:21.234">a </span><span begin="00:00:21.234" end="00:00:21.891">lover</span></p>
</div></body></tt>"""
    lines = parse_ttml(ttml)
    assert len(lines) == 1, lines
    assert lines[0].text == "The club isn't the best place to find a lover"
    assert abs(lines[0].time - 18.234) < 0.001
    assert len(lines[0].words) == 10
    assert lines[0].words[0].text == "The"
    assert abs(lines[0].words[0].time - 18.234) < 0.001
    assert abs(lines[0].words[-1].end - 21.891) < 0.001


def test_parse_ttml_time_formats_and_bg():
    """Bare seconds + M:SS.mmm times, wrapper spans, background vocals."""
    ttml = """<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body><div>
<p begin="33.642" end="35.909"><span begin="33.642" end="33.958">Look</span> <span begin="33.958" end="34.278">at</span> <span begin="1:00.501" end="1:01.000">stars</span></p>
<p begin="40.0" end="44.0"><span begin="40.0" end="42.0">Lead</span> <span ttm:role="x-bg"><span begin="42.0" end="44.0">echo</span></span></p>
</div></body></tt>"""
    lines = parse_ttml(ttml)
    assert len(lines) == 2, lines
    # Bare-seconds times parse; syllable/word spans join on whitespace.
    assert lines[0].text == "Look at stars"
    assert abs(lines[0].words[2].time - 60.501) < 0.001
    # Background vocal span is flagged, lead is not.
    assert lines[1].words[0].text == "Lead"
    assert not lines[1].words[0].is_background
    assert lines[1].words[1].text == "echo"
    assert lines[1].words[1].is_background
    assert parse_ttml("not xml at all") == []
    assert parse_ttml("") == []


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


def test_live_betterlyrics_fetch():
    """Live check: Coldplay — Yellow via the Better Lyrics API should
    come back as TTML with word timings. A None result (401: song not
    cached, no API key) is a SKIP, not a failure — LRCLIB is the
    fallback for exactly that case. Offline also skips."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_betterlyrics("Coldplay", "Yellow", "Parachutes",
                                        269.0, cache_dir=Path(tmp))
    except (urllib.error.URLError, OSError) as e:
        print(f"  SKIP live Better Lyrics test (offline: {e})")
        return "skip"
    if result is None:
        print("  SKIP live Better Lyrics test (uncached — 401 without a key)")
        return "skip"
    assert result.has_word_sync, "Better Lyrics match had no word timings"
    assert len(result.synced_lines) >= 10, f"only {len(result.synced_lines)} lines"
    nwords = sum(len(l.words) for l in result.synced_lines)
    print(f"  live: {result.artist} — {result.track}, "
          f"{len(result.synced_lines)} lines, {nwords} words, source={result.source}")
    return "pass"


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
    test_enhanced_word_tags_parsed,
    test_elrc_two_line_fixture,
    test_word_at_and_has_word_sync,
    test_parse_ttml_docs_example,
    test_parse_ttml_time_formats_and_bg,
    test_out_of_order_lines_sorted,
    test_line_at_with_offset,
    test_live_betterlyrics_fetch,
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
