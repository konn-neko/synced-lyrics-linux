"""Tests for synced-lyrics-linux (lyrics.py only -- no Qt needed).

Run:  python3 test_lyrics.py
  or: python3 -m pytest test_lyrics.py -v        (pytest optional)

The live LRCLIB test is skipped gracefully when offline: it never fails
the suite for lack of network, it prints SKIP instead.
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lyrics as _lyrics_module  # noqa: E402  (for monkeypatching providers)

from lyrics import (  # noqa: E402
    LyricsResult,
    LyricLine,
    LyricWord,
    _REQUEST_LOG,
    cache_lrclib_record,
    clear_track_cache,
    fetch_betterlyrics,
    fetch_kugou,
    fetch_lrclib,
    fetch_qq,
    log_request,
    parse_lrc,
    parse_qrc,
    parse_ttml,
    request_log_text,
    search_all_candidates,
    search_lrclib,
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


def test_parse_ttml_syllable_parts():
    """TTML spans with no whitespace between them merge into one word
    but KEEP their own timings as parts — the mid-word sweep needs
    them. A word split as 'Vir-' + 'tual' is one word, two parts."""
    ttml = """<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
<p begin="1.050" end="2.500"><span begin="1.050" end="1.390">Vir-</span><span begin="1.390" end="1.710">tual </span><span begin="1.710" end="2.140">Insanity</span></p>
</div></body></tt>"""
    lines = parse_ttml(ttml)
    assert len(lines) == 1, lines
    # The hyphen lives in the first span's text, so the joined word
    # is "Vir-tual" — the parser concatenates spans, it doesn't
    # invent a spelling.
    assert lines[0].text == "Vir-tual Insanity"
    assert len(lines[0].words) == 2, lines[0].words
    word = lines[0].words[0]
    assert word.text == "Vir-tual"
    assert len(word.parts) == 2, word.parts
    assert word.parts[0].text == "Vir-"
    assert abs(word.parts[0].time - 1.050) < 0.001
    assert abs(word.parts[0].end - 1.390) < 0.001
    assert word.parts[1].text == "tual"
    assert abs(word.parts[1].time - 1.390) < 0.001
    assert abs(word.parts[1].end - 1.710) < 0.001
    # Second word is a single span: one part — itself.
    assert len(lines[0].words[1].parts) == 1
    assert lines[0].words[1].parts[0].text == "Insanity"


def test_elrc_word_parts():
    """Enhanced-LRC words carry parts=[the word itself], so the sweep
    runs across the whole word (his Jamiroquai file's format)."""
    lines = parse_lrc(
        "[00:01.050]<00:01.050>Virtual <00:01.390>Insanity <00:01.710>- "
        "<00:02.140>Jamiroquai\n")
    assert len(lines) == 1, lines
    assert len(lines[0].words) == 4, lines[0].words
    for w in lines[0].words:
        assert len(w.parts) == 1, w
        assert w.parts[0].text == w.text
        assert w.parts[0].time == w.time
        assert w.parts[0].end == w.end


def test_request_log():
    """log_request records URL + status + body; the log caps at 20."""
    _REQUEST_LOG.clear()
    log_request("https://example.test/a", "HTTP 200", "body-a")
    log_request("https://example.test/b", "HTTP 401", "body-b")
    text = request_log_text()
    assert "https://example.test/a" in text
    assert "HTTP 200" in text and "body-a" in text
    assert "HTTP 401" in text and "body-b" in text
    for i in range(25):
        log_request(f"https://example.test/{i}", "HTTP 200", "x")
    assert len(_REQUEST_LOG) == 20, len(_REQUEST_LOG)
    assert "https://example.test/a" not in request_log_text()
    _REQUEST_LOG.clear()
    assert request_log_text() == "(no HTTP requests yet this run)"


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


def test_cache_roundtrip_keeps_word_tags():
    """A cached ELRC record must reload with its word timings intact.

    (Regression: the old cache write rebuilt syncedLyrics from parsed
    lines, which dropped the <mm:ss.xx> word tags — a cached track
    lost word sync on reload.)
    """
    record = {
        "id": 123, "artistName": "Jamiroquai", "trackName": "Virtual Insanity",
        "albumName": "", "duration": 340.0, "instrumental": False,
        "syncedLyrics": "[00:01.050]<00:01.050>Virtual <00:01.390>Insanity <00:01.710>- <00:02.140>Jamiroquai",
        "plainLyrics": "Virtual Insanity - Jamiroquai",
    }
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        path = cache_lrclib_record(record, "Jamiroquai", "Virtual Insanity",
                                   "", 340.0, cache_dir=cache)
        assert path is not None and Path(path).exists()
        result = fetch_lrclib("Jamiroquai", "Virtual Insanity", "", 340.0,
                              cache_dir=cache)
        assert result is not None and result.source == "cache"
        assert result.has_word_sync, "word timings lost in the cache round-trip"
        assert result.synced_lines[0].words[0].text == "Virtual"
        assert result.raw_record is not None and result.raw_record["id"] == 123
    print("  cache round-trip: OK (ELRC word tags survive)")


def test_clear_track_cache():
    """clear_track_cache removes both providers' files for a track."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        cache_lrclib_record({"id": 1, "artistName": "A", "trackName": "T",
                             "syncedLyrics": "", "plainLyrics": ""},
                            "A", "T", "", None, cache_dir=cache)
        # Fake a Better Lyrics cache file for the same track.
        from lyrics import _betterlyrics_cache_key, _cache_path
        bl = _cache_path(cache, _betterlyrics_cache_key("A", "T", "", None))
        bl.write_text('{"ttml": "<tt></tt>"}', encoding="utf-8")
        assert clear_track_cache("A", "T", "", None, cache_dir=cache) == 2
        assert not bl.exists()
        assert clear_track_cache("A", "T", "", None, cache_dir=cache) == 0
    print("  clear_track_cache: OK (2 files, then 0)")


def test_live_lrclib_search():
    """Live check: searching Coldplay — Yellow returns several matches,
    word-synced ones first. Skipped when offline."""
    try:
        results = search_lrclib("Coldplay", "Yellow", "Parachutes", 269.0)
    except (urllib.error.URLError, OSError) as e:
        print(f"  SKIP live LRCLIB search test (offline: {e})")
        return "skip"
    assert results, "LRCLIB search returned no matches for Coldplay — Yellow"
    assert any(r.has_synced for r in results), "no synced match in search results"
    print(f"  live search: {len(results)} match(es), first = "
          f"{results[0].artist} — {results[0].track} "
          f"(word sync: {results[0].has_word_sync})")
    return "pass"


_QRC_FIXTURE = """[ti:Yellow]
[ar:Coldplay]
[al:The Singles 1999-2006]
[by:]
[offset:0]
[0,8390]Yellow(0,2961) (2961,493)-(3454,494) (3948,493)Coldplay(4441,3949)
[33580,2120]Look (33580,290)at (33870,290)the (34160,290)stars(34450,1250)
[36300,3909]Look (36300,300)how (36600,319)they (36919,360)shine (37279,650)for (37929,1200)you(39129,1080)
"""


def test_parse_qrc_word_timings():
    """QRC: line header [startMs,durMs], word(text) after each word."""
    lines = parse_qrc(_QRC_FIXTURE)
    assert len(lines) == 3, f"expected 3 lines, got {len(lines)}"
    first = lines[0]
    assert first.time == 0.0 and first.text == "Yellow - Coldplay"
    got = [(w.text, round(w.time, 3), round(w.end, 3)) for w in first.words]
    assert got == [("Yellow", 0.0, 2.961), ("-", 3.454, 3.948),
                   ("Coldplay", 4.441, 8.39)], got
    second = lines[1]
    assert second.time == 33.58 and second.text == "Look at the stars"
    assert [w.text for w in second.words] == ["Look", "at", "the", "stars"]
    assert second.words[3].time == 34.45 and second.words[3].end == 35.70
    assert second.words[0].parts[0].text == "Look"  # sweep runs the word
    third = lines[2]
    assert third.text == "Look how they shine for you"
    assert third.words[-1].text == "you" and abs(third.words[-1].end - 40.209) < 0.001
    print("  parse_qrc: OK (3 lines, word timings, whitespace separators skipped)")


def test_parse_qrc_xml_wrapper():
    """QRC inside the QrcInfos XML: LyricContent is extracted with its
    newlines intact (an XML parser would normalize them to spaces)."""
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n<QrcInfos>\n'
           '<QrcHeadInfo SaveTime="1421999193" Version="100"/>\n'
           '<LyricInfo LyricCount="1">\n'
           '<Lyric_1 LyricType="1" LyricContent="'
           + _QRC_FIXTURE.replace('"', "&quot;").replace("\n", "&#10;")
           + '"/>\n</LyricInfo>\n</QrcInfos>')
    lines = parse_qrc(xml)
    assert len(lines) == 3, f"expected 3 lines from XML, got {len(lines)}"
    assert lines[1].text == "Look at the stars"
    print("  parse_qrc XML: OK (LyricContent extracted, newlines kept)")


def test_qq_cache_roundtrip():
    """A cached QQ result reloads with word timings, no network."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        from lyrics import _cache_path, _qq_cache_key
        p = _cache_path(cache, _qq_cache_key("Coldplay", "Yellow", "Parachutes", 269.0))
        p.write_text(json.dumps({"qrc": _QRC_FIXTURE, "fetched_at": 0.0}),
                     encoding="utf-8")
        result = fetch_qq("Coldplay", "Yellow", "Parachutes", 269.0,
                          cache_dir=cache)
        assert result is not None and result.source == "qq-cache"
        assert result.has_word_sync
        assert result.synced_lines[1].text == "Look at the stars"
        assert result.raw_kind == "qrc"
    print("  qq cache round-trip: OK")


def test_search_all_candidates_merges():
    """Picker merge: word-synced first (TTML, QQ, word-tagged LRCLIB),
    then synced, then plain — a provider returning None adds nothing."""
    def _res(source, synced=True, word=False, track="Yellow"):
        line = LyricLine(5.0, "Look at the stars",
                         words=[LyricWord(5.0, 6.0, "Look")] if word else [])
        return LyricsResult(artist="Coldplay", track=track, album="Parachutes",
                            duration=269.0, synced_lines=[line] if synced else [],
                            plain_lyrics="" if synced else "plain words",
                            source=source)

    orig = {name: getattr(_lyrics_module, name) for name in
            ("fetch_betterlyrics", "fetch_qq", "fetch_kugou", "search_lrclib")}
    try:
        _lyrics_module.fetch_betterlyrics = lambda *a, **k: _res("betterlyrics", word=True)
        _lyrics_module.fetch_qq = lambda *a, **k: _res("qq", word=True)
        _lyrics_module.fetch_kugou = lambda *a, **k: None  # no match: adds nothing
        _lyrics_module.search_lrclib = lambda *a, **k: [
            _res("search", word=True, track="Yellow (live)"),
            _res("search", word=False, track="Yellow (demo)")]
        results = search_all_candidates("Coldplay", "Yellow", "Parachutes", 269.0)
    finally:
        for name, fn in orig.items():
            setattr(_lyrics_module, name, fn)
    assert [r.source for r in results] == [
        "betterlyrics", "qq", "search", "search"], [r.source for r in results]
    assert results[0].has_word_sync and results[1].has_word_sync
    assert results[2].has_word_sync and not results[3].has_word_sync
    print("  search_all_candidates: OK (TTML, QQ, LRCLIB word, LRCLIB line)")


def test_live_qq_fetch():
    """Live check: Coldplay — Yellow via QQ returns word-synced QRC.
    Skipped when offline or when QQ has no match."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_qq("Coldplay", "Yellow", "Parachutes", 269.0,
                              cache_dir=Path(tmp))
    except (urllib.error.URLError, OSError) as e:
        print(f"  SKIP live QQ test (offline: {e})")
        return "skip"
    if result is None:
        print("  SKIP live QQ test (no match)")
        return "skip"
    assert result.has_word_sync, "QQ match had no word timings"
    assert len(result.synced_lines) >= 10, f"only {len(result.synced_lines)} lines"
    print(f"  live QQ: {len(result.synced_lines)} lines, word-synced, source={result.source}")
    return "pass"


def test_live_kugou_fetch():
    """Live check: Coldplay — Yellow via Kugou returns line-synced LRC.
    Skipped when offline or when Kugou has no match."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_kugou("Coldplay", "Yellow", "Parachutes", 269.0,
                                 cache_dir=Path(tmp))
    except (urllib.error.URLError, OSError) as e:
        print(f"  SKIP live Kugou test (offline: {e})")
        return "skip"
    if result is None:
        print("  SKIP live Kugou test (no match)")
        return "skip"
    assert result.has_synced, "Kugou match had no synced lyrics"
    print(f"  live Kugou: {len(result.synced_lines)} lines, source={result.source}")
    return "pass"


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
    test_parse_ttml_syllable_parts,
    test_elrc_word_parts,
    test_request_log,
    test_cache_roundtrip_keeps_word_tags,
    test_clear_track_cache,
    test_parse_qrc_word_timings,
    test_parse_qrc_xml_wrapper,
    test_qq_cache_roundtrip,
    test_search_all_candidates_merges,
    test_live_qq_fetch,
    test_live_kugou_fetch,
    test_live_lrclib_search,
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
