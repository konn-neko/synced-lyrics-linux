"""Lyrics clients + parsers for synced-lyrics-linux.

Providers, in order:
  1. Better Lyrics API (https://api.betterlyrics.org) — TTML with
     syllable/word-level timing. Cache-first, no API key: cached songs
     are free, a cache miss without a key returns 401 and we fall back
     to LRCLIB (never fail the track for it).
  2. LRCLIB (https://lrclib.net) — free, no key. Line-level LRC.
     GET /api/get?artist_name=&track_name=&album_name=&duration=
     GET /api/search?q=
Responses carry plainLyrics, syncedLyrics (LRC), instrumental, duration.
Rate limits: ~45 req/min generic, ~90/min identified (Lrclib-Client header).
429 responses carry a Retry-After header -- we honour it and cache nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

LRCLIB_BASE = "https://lrclib.net"
BETTERLYRICS_BASE = "https://api.betterlyrics.org"
CLIENT_HEADER = "synced-lyrics-linux/1.0"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "synced-lyrics-linux"

_TAG_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
_WORD_TAG_RE = re.compile(r"<(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?>")
_META_TAG_RE = re.compile(r"\[(ti|ar|al|by|length|#):[^\]]*\]", re.IGNORECASE)
_TTML_NS = "http://www.w3.org/ns/ttml"
_TTML_META_NS = "http://www.w3.org/ns/ttml#metadata"


def _frac_to_seconds(frac: str | None) -> float:
    """'.x' = tenths, '.xx' = hundredths, '.xxx' = milliseconds."""
    if not frac:
        return 0.0
    if len(frac) == 1:
        return int(frac) / 10.0
    if len(frac) == 2:
        return int(frac) / 100.0
    return int(frac) / 1000.0


@dataclass
class LyricWord:
    time: float  # seconds, start of the word
    end: float   # seconds, end of the word
    text: str
    is_background: bool = False


@dataclass
class LyricLine:
    time: float  # seconds
    text: str
    words: list[LyricWord] = field(default_factory=list)


@dataclass
class LyricsResult:
    artist: str
    track: str
    album: str = ""
    duration: float | None = None
    synced_lines: list[LyricLine] = field(default_factory=list)
    plain_lyrics: str = ""
    instrumental: bool = False
    source: str = "lrclib"  # lrclib | cache | search | none
    lrclib_id: int | None = None

    @property
    def has_synced(self) -> bool:
        return bool(self.synced_lines)

    @property
    def has_word_sync(self) -> bool:
        """True when at least one line carries per-word timings."""
        return any(line.words for line in self.synced_lines)

    def line_at(self, position: float, offset: float = 0.0) -> int:
        """Index of the line active at `position` (offset applied, seconds).

        Returns -1 if no line has started yet.
        """
        pos = position + offset
        idx = -1
        for i, line in enumerate(self.synced_lines):
            if line.time <= pos:
                idx = i
            else:
                break
        return idx

    def word_at(self, line_idx: int, position: float, offset: float = 0.0) -> int:
        """Index of the word active at `position` within line `line_idx`.

        Returns -1 when the line has no word timings, or no word has
        started yet. A word is active once its start time has passed.
        """
        if not (0 <= line_idx < len(self.synced_lines)):
            return -1
        words = self.synced_lines[line_idx].words
        if not words:
            return -1
        pos = position + offset
        idx = -1
        for i, word in enumerate(words):
            if word.time <= pos:
                idx = i
            else:
                break
        return idx


class LRCLIBRateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"LRCLIB rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def _word_time(minute: str, second: str, frac: str | None) -> float:
    return int(minute) * 60 + int(second) + _frac_to_seconds(frac)


def _finish_word_ends(lines: list[LyricLine]) -> None:
    """Fill in each word's end time: the next word's start, or the next
    line's start for the last word of a line (last line: +3 s)."""
    for i, line in enumerate(lines):
        if not line.words:
            continue
        next_line_time = lines[i + 1].time if i + 1 < len(lines) else line.time + 3.0
        for j, word in enumerate(line.words):
            if j + 1 < len(line.words):
                word.end = line.words[j + 1].time
            else:
                word.end = max(word.time, next_line_time)


def parse_lrc(text: str) -> list[LyricLine]:
    """Parse LRC text into sorted [LyricLine].

    Handles: [mm:ss.xx], [mm:ss], multiple timestamps per line,
    [offset:+/-ms], metadata tags ([ti:], [ar:] ... ignored),
    empty / blank lines (skipped), and Enhanced-LRC word tags
    <mm:ss.xx>word — these are parsed into per-word timings on the
    line (word-by-word highlight), NOT stripped. A line with no word
    tags keeps words=[] (line-only sync).
    """
    if not text:
        return []
    offset_ms = 0.0
    m = _OFFSET_RE.search(text)
    if m:
        try:
            offset_ms = float(m.group(1))
        except ValueError:
            offset_ms = 0.0

    lines: list[LyricLine] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        tags = _TAG_RE.findall(line)
        if not tags:
            continue  # metadata-only or untimed line; not a synced line
        # Text is everything after the last timestamp tag.
        last_end = 0
        for mm in _TAG_RE.finditer(line):
            last_end = mm.end()
        body = line[last_end:].strip()
        body = _META_TAG_RE.sub("", body).strip()
        if not body:
            continue
        # Words: <time>word chunks. A chunk's text runs to the next
        # word tag (or end of line); trailing spaces are separators.
        words: list[tuple[float, str]] = []
        word_matches = list(_WORD_TAG_RE.finditer(body))
        if word_matches:
            for k, wm in enumerate(word_matches):
                chunk_start = wm.end()
                chunk_end = (word_matches[k + 1].start()
                             if k + 1 < len(word_matches) else len(body))
                wtext = body[chunk_start:chunk_end].strip()
                if not wtext:
                    continue
                wt = _word_time(wm.group(1), wm.group(2), wm.group(3))
                words.append((wt, wtext))
        if words:
            plain = " ".join(w for _, w in words)
        else:
            plain = _WORD_TAG_RE.sub("", body)
            plain = re.sub(r"\s+", " ", plain).strip()
        if not plain:
            continue
        base_time = _word_time(tags[0][0], tags[0][1], tags[0][2])
        for minute, second, frac in tags:
            total = _word_time(minute, second, frac) + offset_ms / 1000.0
            if total < 0:
                total = 0.0
            shift = total - (base_time + offset_ms / 1000.0)
            line_words = [
                LyricWord(time=max(0.0, wt + shift + offset_ms / 1000.0),
                          end=0.0, text=wtext)
                for wt, wtext in words
            ]
            lines.append(LyricLine(time=total, text=plain, words=line_words))
    lines.sort(key=lambda l: l.time)
    _finish_word_ends(lines)
    return lines


# ------------------------------------------------------------------ TTML ---
def _parse_ttml_time(value: str | None) -> float | None:
    """TTML times: 'HH:MM:SS.mmm', 'M:SS.mmm', 'SS.mmm' (bare seconds)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parts = value.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


def _collect_ttml_spans(elem, is_background: bool, out: list) -> None:
    """Recursively collect timed <span>s under elem.

    A span with a `begin` is a word/syllable. A span without one is a
    wrapper (e.g. background vocals) — recurse into it, propagating
    the background flag (ttm:role="x-bg"). Whitespace between spans
    is the space between words: a span whose text ends in whitespace,
    or whose tail holds whitespace, ends a word; spans with neither
    continue the same word (syllables). Text is never stripped of
    internal spacing and the document is never pretty-printed.
    """
    for child in elem:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag != "span":
            _collect_ttml_spans(child, is_background, out)
            continue
        bg = is_background or child.get(f"{{{_TTML_META_NS}}}role") == "x-bg"
        begin = _parse_ttml_time(child.get("begin"))
        if begin is None:
            _collect_ttml_spans(child, bg, out)
            continue
        end = _parse_ttml_time(child.get("end"))
        text = child.text or ""
        tail = child.tail or ""
        out.append({
            "begin": begin,
            "end": end if end is not None else begin,
            "text": text,
            "tail": tail,
            "is_background": bg,
        })


def parse_ttml(text: str) -> list[LyricLine]:
    """Parse a Better Lyrics TTML document into [LyricLine] with words.

    Each <p> is a line (time = its begin), each timed <span> inside it
    is a word/syllable (begin/end). Consecutive spans with no
    whitespace between them are merged into one word (syllables of the
    same word); a whitespace tail or trailing space in the span text
    ends a word. Lines with no timed spans keep words=[].
    """
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    ns = {"tt": _TTML_NS}
    body = root.find("tt:body", ns)
    if body is None:
        body = root
    lines: list[LyricLine] = []
    for p in body.iter(f"{{{_TTML_NS}}}p"):
        begin = _parse_ttml_time(p.get("begin"))
        raw_spans: list[dict] = []
        _collect_ttml_spans(p, False, raw_spans)
        words: list[LyricWord] = []
        cur_text = ""
        cur_begin: float | None = None
        cur_end: float | None = None
        cur_bg = False
        for span in raw_spans:
            if cur_begin is None:
                cur_begin, cur_end, cur_bg = span["begin"], span["end"], span["is_background"]
            cur_text += span["text"]
            cur_end = span["end"]
            cur_bg = cur_bg or span["is_background"]
            # Word boundary: the span text ends in whitespace, or its
            # tail holds whitespace (the space between two words).
            boundary = (span["text"].endswith((" ", "\t", "\n"))
                        or any(c in span["tail"] for c in " \t\n"))
            if span["tail"].strip():
                # Punctuation / text outside spans attaches to the word.
                cur_text += span["tail"].strip()
            if boundary:
                words.append(LyricWord(time=cur_begin, end=cur_end or cur_begin,
                                       text=cur_text.strip(),
                                       is_background=cur_bg))
                cur_text, cur_begin, cur_end, cur_bg = "", None, None, False
        if cur_text.strip() and cur_begin is not None:
            words.append(LyricWord(time=cur_begin, end=cur_end or cur_begin,
                                   text=cur_text.strip(),
                                   is_background=cur_bg))
        if words:
            line_text = " ".join(w.text for w in words)
            line_time = begin if begin is not None else words[0].time
        else:
            line_text = "".join(p.itertext()).strip()
            line_time = begin if begin is not None else 0.0
        if not line_text:
            continue
        lines.append(LyricLine(time=line_time, text=line_text, words=words))
    lines.sort(key=lambda l: l.time)
    return lines


def _betterlyrics_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "bl-" + _cache_key(artist, track, album, duration)


def fetch_betterlyrics(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch word-synced lyrics from the Better Lyrics API (TTML).

    Cache-first, no API key: cached songs are free. A cache miss
    without a key returns 401, and any 429 / network error returns
    None — the caller falls back to LRCLIB, the track never fails
    for this. Only artist/title/album/duration are sent.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _betterlyrics_cache_key(artist, track, album, duration)

    if use_cache:
        try:
            p = _cache_path(cache, key)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                ttml = data.get("ttml") or ""
                lines = parse_ttml(ttml)
                if lines:
                    return LyricsResult(
                        artist=artist, track=track, album=album,
                        duration=duration, synced_lines=lines,
                        source="betterlyrics-cache")
        except Exception:
            pass  # corrupt cache: fall through to network

    params = {"s": track, "a": artist}
    if album:
        params["al"] = album
    if duration is not None:
        params["d"] = str(int(round(duration)))
    url = f"{BETTERLYRICS_BASE}/getLyrics?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": CLIENT_HEADER, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 = cache miss without an API key (keys are not being
        # issued); 429 = rate limited. Either way: fall back, don't fail.
        return None
    except Exception:
        return None
    ttml = payload.get("ttml") if isinstance(payload, dict) else None
    if not ttml:
        return None
    lines = parse_ttml(ttml)
    if not lines:
        return None
    result = LyricsResult(
        artist=artist, track=track, album=album, duration=duration,
        synced_lines=lines, source="betterlyrics")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _cache_path(cache, key).write_text(
            json.dumps({"ttml": ttml, "fetched_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return result


def _cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    norm = f"{artist.strip().lower()}|{track.strip().lower()}|{album.strip().lower()}|"
    if duration is not None:
        norm += str(int(round(duration)))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def _get_json(url: str, timeout: float = 10.0) -> dict | list:
    req = urllib.request.Request(
        url, headers={"Lrclib-Client": CLIENT_HEADER, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry) if retry else 60.0
            except ValueError:
                wait = 60.0
            raise LRCLIBRateLimited(wait)
        raise


def _result_from_record(record: dict, artist: str, track: str, source: str) -> LyricsResult:
    synced_raw = record.get("syncedLyrics") or ""
    return LyricsResult(
        artist=record.get("artistName") or artist,
        track=record.get("trackName") or track,
        album=record.get("albumName") or "",
        duration=record.get("duration"),
        synced_lines=parse_lrc(synced_raw) if synced_raw else [],
        plain_lyrics=record.get("plainLyrics") or "",
        instrumental=bool(record.get("instrumental")),
        source=source,
        lrclib_id=record.get("id"),
    )


def fetch_lrclib(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch lyrics for a track. Returns None when LRCLIB has no match.

    Order: disk cache -> /api/get -> /api/search (best duration match).
    Raises LRCLIBRateLimited on 429; other HTTP/network errors propagate
    as urllib.error.URLError / HTTPError so callers can show them.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _cache_key(artist, track, album, duration)

    if use_cache:
        try:
            p = _cache_path(cache, key)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                # Cached records store the raw LRCLIB record + source marker.
                rec = data.get("record") or {}
                result = _result_from_record(rec, artist, track, "cache")
                result.source = "cache"
                return result
        except Exception:
            pass  # corrupt cache: fall through to network

    params = {
        "artist_name": artist,
        "track_name": track,
    }
    if album:
        params["album_name"] = album
    if duration is not None:
        params["duration"] = str(int(round(duration)))
    url = f"{LRCLIB_BASE}/api/get?{urllib.parse.urlencode(params)}"
    try:
        record = _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        record = None

    if isinstance(record, dict) and record.get("id"):
        result = _result_from_record(record, artist, track, "lrclib")
    else:
        # Fallback: /api/search, pick closest duration match with synced lyrics.
        q = f"{artist} {track}"
        search_url = f"{LRCLIB_BASE}/api/search?{urllib.parse.urlencode({'q': q})}"
        results = _get_json(search_url)
        if not isinstance(results, list) or not results:
            return None
        best = None
        best_score = None
        for rec in results:
            if not isinstance(rec, dict):
                continue
            score = 0.0
            if duration is not None and rec.get("duration"):
                score = abs(float(rec["duration"]) - float(duration))
            if rec.get("syncedLyrics"):
                score -= 1000  # strongly prefer synced over plain-only
            if best_score is None or score < best_score:
                best, best_score = rec, score
        if best is None:
            return None
        result = _result_from_record(best, artist, track, "search")

    # Cache the raw record (not the parsed lines) so cache reads re-parse identically.
    try:
        cache.mkdir(parents=True, exist_ok=True)
        raw = {
            "artistName": result.artist,
            "trackName": result.track,
            "albumName": result.album,
            "duration": result.duration,
            "syncedLyrics": "\n".join(
                f"[{int(l.time // 60):02d}:{l.time % 60:05.2f}] {l.text}"
                for l in result.synced_lines
            ) if result.synced_lines else None,
            "plainLyrics": result.plain_lyrics or None,
            "instrumental": result.instrumental,
            "id": result.lrclib_id,
        }
        _cache_path(cache, key).write_text(
            json.dumps({"record": raw, "fetched_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return result


# --- Demo fixture: a short, timestamped excerpt (not the full song) ---
DEMO_ARTIST = "Coldplay"
DEMO_TRACK = "Yellow"
DEMO_ALBUM = "Parachutes"
DEMO_DURATION = 269.0
DEMO_LRC = """[00:05.00] Look at the stars
[00:09.50] Look how they shine for you
[00:14.00] And everything you do
[00:18.50] Yeah, they were all yellow
[00:27.00] I came along
[00:31.50] I wrote a song for you
[00:36.00] And all the things you do
[00:40.50] And it was called Yellow
[00:49.00] So then I took my turn
[00:53.50] Oh, what a thing to have done
[00:58.00] And it was all yellow
[01:06.00] Your skin, oh yeah, your skin and bones
[01:11.00] Turn into something beautiful
[01:16.00] And you know, you know I love you so
[01:21.00] You know I love you so
[02:00.00] (demo) — this is a short sample, not the full song
[02:05.00] Real tracks are fetched live from LRCLIB
"""


def _proportional_words(line: LyricLine, end_time: float) -> list[LyricWord]:
    """Split a line's words across [line.time, end_time], weighted by
    word length — demo-only filler so --demo shows the word highlight
    with no network. Real tracks use real timings (TTML / ELRC)."""
    parts = line.text.split()
    if not parts:
        return []
    weights = [len(p) + 1 for p in parts]  # +1 for the space after it
    total_w = sum(weights)
    span = max(0.1, end_time - line.time)
    words: list[LyricWord] = []
    t = line.time
    for i, part in enumerate(parts):
        dur = span * weights[i] / total_w
        words.append(LyricWord(time=t, end=t + dur, text=part))
        t += dur
    return words


def demo_result() -> LyricsResult:
    lines = parse_lrc(DEMO_LRC)
    for i, line in enumerate(lines):
        end = lines[i + 1].time if i + 1 < len(lines) else line.time + 4.0
        line.words = _proportional_words(line, end)
    return LyricsResult(
        artist=DEMO_ARTIST,
        track=DEMO_TRACK,
        album=DEMO_ALBUM,
        duration=DEMO_DURATION,
        synced_lines=lines,
        plain_lyrics="",
        instrumental=False,
        source="demo",
    )
