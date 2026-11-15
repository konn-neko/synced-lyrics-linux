"""Lyrics clients + parsers for synced-lyrics-linux.

Providers, in order:
  1. Better Lyrics API (https://api.betterlyrics.org) — TTML with
     syllable/word-level timing. Cache-first, no API key: cached songs
     are free, a cache miss without a key returns 401 and we fall back
     (never fail the track for it).
  2. Better Lyrics QQ provider (/qq/getLyrics) — QRC, word-level
     timing (QQ Music). Same cache-first, no-key terms as TTML.
  3. LRCLIB (https://lrclib.net) — free, no key. Line-level LRC,
     some records carry Enhanced-LRC word tags.
     GET /api/get?artist_name=&track_name=&album_name=&duration=
     GET /api/search?q=
  4. Better Lyrics Kugou provider (/kugou/getLyrics) — line-level
     LRC. Last resort when LRCLIB has nothing.
Responses carry plainLyrics, syncedLyrics (LRC), instrumental, duration.
Rate limits: ~45 req/min generic, ~90/min identified (Lrclib-Client header).
429 responses carry a Retry-After header -- we honour it and cache nothing.
"""
from __future__ import annotations

import hashlib
import html
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


# ------------------------------------------------------------ request log ---
# Every HTTP request this module makes, with its response, for the
# Help > Lyrics debug view. Callers that fetch other URLs (album art)
# log through the same list. Bodies are capped per entry; the log
# keeps the last 20 requests.
_REQUEST_LOG: list[dict] = []
_REQUEST_LOG_MAX = 20
_REQUEST_LOG_BODY_MAX = 200_000


def log_request(url: str, status: str, body: str = "") -> None:
    """Record one HTTP request + response for the debug view."""
    if not isinstance(body, str):
        body = str(body)
    _REQUEST_LOG.append({
        "time": time.strftime("%H:%M:%S", time.localtime()),
        "url": url,
        "status": status,
        "body": body[:_REQUEST_LOG_BODY_MAX],
    })
    del _REQUEST_LOG[:-_REQUEST_LOG_MAX]


def request_log_text() -> str:
    """The request log as plain text, newest last."""
    if not _REQUEST_LOG:
        return "(no HTTP requests yet this run)"
    blocks = []
    for e in _REQUEST_LOG:
        blocks.append(f"[{e['time']}] GET {e['url']}\n-> {e['status']}\n{e['body']}")
    return "\n\n".join(blocks)


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
    # Syllable-level parts (TTML spans inside the word). A word with
    # no parts is one unit — the fill sweep runs across the whole word.
    # ELRC words and demo words carry parts=[the word itself].
    parts: list[LyricWord] = field(default_factory=list)


@dataclass
class LyricLine:
    time: float  # seconds
    text: str
    words: list[LyricWord] = field(default_factory=list)


def is_censored_text(text: str) -> bool:
    """True when lyric text carries asterisk-censored words.

    Providers that censor explicit lyrics write them as `f**k`,
    `Sh*t`, `b**ch`, `ni**a` or a bare `****` — a token containing
    `*` next to letters, or a token of two or more asterisks. A lone
    `*` (e.g. a multiplication sign) does not count. A track that
    legitimately uses `***` as a separator would false-positive;
    that is rare, and the only cost is a "censored" tag and a lower
    sort rank in the picker.
    """
    for token in re.split(r"\s+", text or ""):
        if not token:
            continue
        if "*" not in token:
            continue
        if len(token) >= 2 and token.strip("*") == "":
            return True  # bare "****" standing in for a word
        if any(c.isalnum() for c in token):
            return True  # "f**k", "Sh*t", "b**ch" ...
    return False


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
    # The raw lyrics file exactly as received (TTML or LRC text), for
    # the Help > Lyrics debug view. Empty for plain-only results.
    raw_text: str = ""
    raw_kind: str = ""  # "ttml" | "lrc" | ""
    cache_file: str = ""  # cache file this result came from / was saved to
    # The raw LRCLIB record this result was built from (None for
    # Better Lyrics / demo). Kept so a picked search candidate can be
    # cached verbatim under the current track's key — the raw
    # syncedLyrics keeps its Enhanced-LRC word tags, which a rebuild
    # from parsed lines would lose.
    raw_record: dict | None = None

    @property
    def has_synced(self) -> bool:
        return bool(self.synced_lines)

    @property
    def has_word_sync(self) -> bool:
        """True when at least one line carries per-word timings."""
        return any(line.words for line in self.synced_lines)

    @property
    def is_censored(self) -> bool:
        """True when the raw lyrics text carries asterisk-censored
        words (see `is_censored_text`). Checked against `raw_text`,
        which every provider sets — so it works for TTML, QRC, LRC
        and LRCLIB records alike."""
        return is_censored_text(self.raw_text or "")

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
            if not word.parts:
                # ELRC word: one part — the word itself.
                word.parts = [LyricWord(time=word.time, end=word.end,
                                        text=word.text,
                                        is_background=word.is_background)]


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
        # Syllable parts of the word being built: each timed span is
        # one part, kept with its own begin/end so the fill sweep can
        # light a word syllable by syllable instead of all at once.
        cur_parts: list[LyricWord] = []
        for span in raw_spans:
            if cur_begin is None:
                cur_begin, cur_end, cur_bg = span["begin"], span["end"], span["is_background"]
            cur_text += span["text"]
            cur_end = span["end"]
            cur_bg = cur_bg or span["is_background"]
            ptext = span["text"].strip()
            if ptext:
                cur_parts.append(LyricWord(time=span["begin"], end=span["end"],
                                           text=ptext,
                                           is_background=span["is_background"]))
            # Word boundary: the span text ends in whitespace, or its
            # tail holds whitespace (the space between two words).
            boundary = (span["text"].endswith((" ", "\t", "\n"))
                        or any(c in span["tail"] for c in " \t\n"))
            if span["tail"].strip():
                # Punctuation / text outside spans attaches to the word.
                cur_text += span["tail"].strip()
                if cur_parts:
                    cur_parts[-1].text += span["tail"].strip()
            if boundary:
                words.append(LyricWord(time=cur_begin, end=cur_end or cur_begin,
                                       text=cur_text.strip(),
                                       is_background=cur_bg,
                                       parts=cur_parts))
                cur_text, cur_begin, cur_end, cur_bg = "", None, None, False
                cur_parts = []
        if cur_text.strip() and cur_begin is not None:
            words.append(LyricWord(time=cur_begin, end=cur_end or cur_begin,
                                   text=cur_text.strip(),
                                   is_background=cur_bg,
                                   parts=cur_parts))
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


# ------------------------------------------------------------------- QRC ---
_QRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_QRC_WORD_RE = re.compile(r"(.+?)\((\d+),(\d+)\)")


def parse_qrc(text: str) -> list[LyricLine]:
    """Parse QQ Music QRC (word-level) into [LyricLine].

    Accepts the raw LyricContent string or the full QrcInfos XML (the
    LyricContent attribute is extracted first; LyricType 1 preferred).
    Each line is `[startMs,durationMs]` followed by
    `word(startMs,durationMs)` segments — the timing follows the word
    text, and a segment that is only whitespace is the separator
    between two words, not a word. Times are milliseconds. Metadata
    lines ([ti:], [ar:] ...) have no [start,duration] header and are
    skipped. Word ends come from the QRC durations (not inferred).
    """
    if not text:
        return []
    content = text
    if "<QrcInfos" in text or "<Lyric_" in text:
        # Extract LyricContent from the raw XML string with a regex:
        # an XML parser would normalize the newlines inside the
        # attribute value to spaces (attribute-value normalization),
        # merging every lyric line into one. The regex keeps them.
        m = re.search(r'LyricContent="(.*?)"', text, re.DOTALL)
        if m:
            content = html.unescape(m.group(1))
        else:
            # Fallback: parse the XML and take the first Lyric_*.
            try:
                root = ET.fromstring(text)
                lyric = None
                for elem in root.iter():
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if tag.startswith("Lyric_"):
                        if lyric is None:
                            lyric = elem
                        if elem.get("LyricType") == "1":
                            lyric = elem
                            break
                if lyric is not None:
                    content = lyric.get("LyricContent") or ""
            except ET.ParseError:
                pass
    lines: list[LyricLine] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _QRC_LINE_RE.match(line)
        if not m:
            continue
        start_ms = int(m.group(1))
        body = m.group(3)
        words: list[LyricWord] = []
        for wm in _QRC_WORD_RE.finditer(body):
            wtext = wm.group(1).strip()
            if not wtext:
                continue  # whitespace-only segment: word separator
            wstart = int(wm.group(2)) / 1000.0
            wend = wstart + int(wm.group(3)) / 1000.0
            w = LyricWord(time=wstart, end=wend, text=wtext)
            w.parts = [LyricWord(time=wstart, end=wend, text=wtext)]
            words.append(w)
        if not words:
            continue
        lines.append(LyricLine(time=start_ms / 1000.0,
                               text=" ".join(w.text for w in words),
                               words=words))
    lines.sort(key=lambda l: l.time)
    return lines


def _betterlyrics_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "bl-" + _cache_key(artist, track, album, duration)


def _qq_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "qq-" + _cache_key(artist, track, album, duration)


def _kg_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "kg-" + _cache_key(artist, track, album, duration)


def _fetch_betterlyrics_provider(
    provider: str,
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> dict | None:
    """One GET to a Better Lyrics provider endpoint (/qq, /kugou...).

    Returns the parsed JSON payload, or None on 401/429/network error
    (the caller falls through to the next provider — never fail the
    track for one provider). Every request is logged for the debug view.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    params = {"s": track, "a": artist}
    if album:
        params["al"] = album
    if duration is not None:
        params["d"] = str(int(round(duration)))
    url = (f"{BETTERLYRICS_BASE}/{provider}/getLyrics?"
           f"{urllib.parse.urlencode(params)}")
    req = urllib.request.Request(
        url, headers={"User-Agent": CLIENT_HEADER, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            raw = resp.read().decode("utf-8")
            log_request(url, f"HTTP {resp.status}", raw)
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        log_request(url, f"HTTP {e.code}", body)
        return None
    except Exception as e:
        log_request(url, "network error", str(e))
        return None


def fetch_qq(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch word-synced lyrics from QQ Music via Better Lyrics (/qq).

    The response carries QRC XML — word-level timings, like TTML but
    from QQ. Cache-first, no key (same terms as the TTML endpoint).
    Returns None when QQ has no match for the track.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _qq_cache_key(artist, track, album, duration)

    def _from_qrc(qrc: str, source: str) -> LyricsResult | None:
        lines = parse_qrc(qrc)
        if not lines:
            return None
        return LyricsResult(
            artist=artist, track=track, album=album, duration=duration,
            synced_lines=lines, source=source, raw_text=qrc, raw_kind="qrc",
            cache_file=str(_cache_path(cache, key)))

    if use_cache:
        try:
            p = _cache_path(cache, key)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                result = _from_qrc(data.get("qrc") or "", "qq-cache")
                if result is not None:
                    return result
        except Exception:
            pass  # corrupt cache: fall through to network

    payload = _fetch_betterlyrics_provider("qq", artist, track, album, duration)
    if payload is None:
        return None
    qrc = payload.get("lyrics") or ""
    if not qrc:
        return None
    result = _from_qrc(qrc, "qq")
    if result is None:
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _cache_path(cache, key).write_text(
            json.dumps({"qrc": qrc, "fetched_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return result


def fetch_kugou(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch line-synced lyrics from Kugou via Better Lyrics (/kugou).

    Kugou's endpoint returns plain line-level LRC (no word tags) —
    it is a coverage fallback for songs LRCLIB lacks, not a word-sync
    source. Cache-first, no key. Returns None when Kugou has no match.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _kg_cache_key(artist, track, album, duration)

    def _from_lrc(lrc: str, source: str) -> LyricsResult | None:
        lines = parse_lrc(lrc)
        if not lines:
            return None
        return LyricsResult(
            artist=artist, track=track, album=album, duration=duration,
            synced_lines=lines, source=source, raw_text=lrc, raw_kind="lrc",
            cache_file=str(_cache_path(cache, key)))

    if use_cache:
        try:
            p = _cache_path(cache, key)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                result = _from_lrc(data.get("lrc") or "", "kugou-cache")
                if result is not None:
                    return result
        except Exception:
            pass  # corrupt cache: fall through to network

    payload = _fetch_betterlyrics_provider("kugou", artist, track, album, duration)
    if payload is None:
        return None
    lrc = payload.get("lyrics") or ""
    if not lrc:
        return None
    result = _from_lrc(lrc, "kugou")
    if result is None:
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _cache_path(cache, key).write_text(
            json.dumps({"lrc": lrc, "fetched_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return result


def search_all_candidates(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
) -> list[LyricsResult]:
    """All lyric candidates for the picker, word-synced first.

    Better Lyrics TTML (single best match) + QQ QRC (word-level) +
    Kugou LRC + LRCLIB search results. Each provider is best-effort:
    one returning None just contributes nothing. Sorted: word-synced
    first (TTML, then QQ, then word-tagged LRCLIB), then synced,
    then plain-only — provider order kept within each rank.
    """
    candidates: list[LyricsResult] = []
    for fn in (fetch_betterlyrics, fetch_qq, fetch_kugou):
        try:
            result = fn(artist, track, album, duration)
        except Exception:
            result = None
        if result is not None and (result.has_synced or result.plain_lyrics
                                   or result.instrumental):
            candidates.append(result)
    try:
        candidates.extend(search_lrclib(artist, track, album, duration))
    except Exception:
        pass

    def _provider_rank(r: LyricsResult) -> int:
        if r.source.startswith("betterlyrics"):
            return 0
        if r.source.startswith("qq"):
            return 1
        if r.source == "search":
            return 2
        return 3

    def _rank(r: LyricsResult):
        sync = 0 if r.has_word_sync else (1 if r.has_synced else 2)
        # Uncensored above censored at the same sync level: a
        # word-synced but asterisked QQ match ranks below an
        # uncensored word-synced TTML match, and below nothing else.
        censored = 1 if r.is_censored else 0
        return (sync, censored, _provider_rank(r))

    candidates.sort(key=_rank)
    return candidates


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
                        source="betterlyrics-cache", raw_text=ttml,
                        raw_kind="ttml", cache_file=str(p))
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
            raw = resp.read().decode("utf-8")
            log_request(url, f"HTTP {resp.status}", raw)
            payload = json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        log_request(url, f"HTTP {e.code}", body)
        # 401 = cache miss without an API key (keys are not being
        # issued); 429 = rate limited. Either way: fall back, don't fail.
        return None
    except Exception as e:
        log_request(url, "network error", str(e))
        return None
    ttml = payload.get("ttml") if isinstance(payload, dict) else None
    if not ttml:
        return None
    lines = parse_ttml(ttml)
    if not lines:
        return None
    result = LyricsResult(
        artist=artist, track=track, album=album, duration=duration,
        synced_lines=lines, source="betterlyrics", raw_text=ttml,
        raw_kind="ttml", cache_file=str(_cache_path(cache, key)))
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
            raw = resp.read().decode("utf-8")
            log_request(url, f"HTTP {resp.status}", raw)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        log_request(url, f"HTTP {e.code}", body)
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
        raw_text=synced_raw,
        raw_kind="lrc" if synced_raw else "",
        raw_record=record,
    )


def search_lrclib(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    limit: int = 10,
) -> list[LyricsResult]:
    """Search LRCLIB for all matches of a track (the picker list).

    GET /api/search?q=artist track — the records already carry their
    syncedLyrics, so a picked candidate loads without another fetch.
    Sorted: word-synced first, then synced, then plain-only, with
    duration closeness as the tiebreak. Raises LRCLIBRateLimited on
    429; other HTTP/network errors propagate to the caller.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return []
    q = f"{artist} {track}"
    if album:
        q += f" {album.strip()}"
    url = f"{LRCLIB_BASE}/api/search?{urllib.parse.urlencode({'q': q})}"
    results = _get_json(url)
    if not isinstance(results, list):
        return []

    def sort_key(rec: dict):
        r = _result_from_record(rec, artist, track, "search")
        rank = 0 if r.has_word_sync else (1 if r.has_synced else 2)
        dur_diff = 0.0
        if duration is not None and rec.get("duration"):
            try:
                dur_diff = abs(float(rec["duration"]) - float(duration))
            except (TypeError, ValueError):
                dur_diff = 0.0
        return (rank, dur_diff)

    records = [r for r in results if isinstance(r, dict)]
    records.sort(key=sort_key)
    return [_result_from_record(r, artist, track, "search")
            for r in records[:limit]]


def clear_track_cache(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
) -> int:
    """Delete all cache files for a track (Better Lyrics TTML + QQ +
    Kugou + LRCLIB).

    Used by a forced re-pull: the cached lyrics were wrong, so they
    must not survive a failed re-fetch either. Returns how many files
    were removed.
    """
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    removed = 0
    for key in (_betterlyrics_cache_key(artist, track, album, duration),
                _qq_cache_key(artist, track, album, duration),
                _kg_cache_key(artist, track, album, duration),
                _cache_key(artist, track, album, duration)):
        try:
            p = _cache_path(cache, key)
            if p.exists():
                p.unlink()
                removed += 1
        except Exception:
            pass
    return removed


def cache_lrclib_record(
    record: dict,
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
) -> str | None:
    """Cache a raw LRCLIB record under the given track's key.

    The raw record is stored verbatim (not rebuilt from parsed lines)
    so Enhanced-LRC word tags survive the round-trip. Returns the
    cache file path, or None if the write failed.
    """
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _cache_key(artist, track, album, duration)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        p = _cache_path(cache, key)
        p.write_text(
            json.dumps({"record": record, "fetched_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
        return str(p)
    except Exception:
        return None


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
                result.cache_file = str(p)
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
        raw_record = record
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
        raw_record = best

    result.cache_file = str(_cache_path(cache, key))
    # Cache the raw record verbatim (not rebuilt from parsed lines)
    # so Enhanced-LRC word tags survive the round-trip.
    cache_lrclib_record(raw_record, artist, track, album, duration, cache)
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
        w = LyricWord(time=t, end=t + dur, text=part)
        w.parts = [LyricWord(time=t, end=t + dur, text=part)]
        words.append(w)
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
        raw_text=DEMO_LRC,
        raw_kind="lrc",
    )
