"""Lyrics clients + parsers for synced-lyrics-linux.

Providers, in order:
  1. Better Lyrics API (https://api.betterlyrics.org) — TTML with
     syllable/word-level timing. Cache-first, no API key: cached songs
     are free, a cache miss without a key returns 401 and we fall back
     (never fail the track for it).
  2. Unison (https://unison.betterlyrics.org) — Better Lyrics'
     community uploads, TTML or LRC, whatever was submitted.
     GET /lyrics?song=&artist=&album=&duration= — no key.
  3. AMLL TTML Database (https://api.amll.dev) — hand-timed
     syllable TTML, public domain, volunteer-run. Search returns
     metadata only; lyrics come from a second GET by id.
     GET /v1/lyrics/search?musicName=&artistName=&pageSize=
     GET /v1/lyrics/get?id= — no key. Small corpus (a few thousand
     songs), so most tracks miss here and fall through.
  4. Better Lyrics QQ provider (/qq/getLyrics) — QRC, word-level
     timing (QQ Music). Same cache-first, no-key terms as TTML.
  5. NetEase Cloud Music (https://music.163.com) — word-level yrc
     plus line LRC and translations. Unofficial web-player
     endpoints, no key: GET /api/search/get?s=&type=1&limit=&offset=
     then GET /api/song/lyric?id=&lv=-1&kv=-1&tv=-1. yrc is empty
     for many tracks (then the LRC is used); some recordings have
     no lyrics at all ("暂无歌词").
  6. LRCLIB (https://lrclib.net) — free, no key. Line-level LRC,
     some records carry Enhanced-LRC word tags.
     GET /api/get?artist_name=&track_name=&album_name=&duration=
     GET /api/search?q=
  7. Better Lyrics Kugou provider (/kugou/getLyrics) — line-level
     LRC. Last resort when LRCLIB has nothing.
Responses carry plainLyrics, syncedLyrics (LRC), instrumental, duration.
Rate limits: ~45 req/min generic, ~90/min identified (Lrclib-Client header).
429 responses carry a Retry-After header -- we honour it and cache nothing.
"""
from __future__ import annotations

import difflib
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
UNISON_BASE = "https://unison.betterlyrics.org"
AMLL_BASE = "https://api.amll.dev"
NETEASE_BASE = "https://music.163.com"
CLIENT_HEADER = "synced-lyrics-linux/1.0"

# The provider registry. Key is the stable id used in config
# ("provider_order" / "provider_enabled") and in LyricsResult.source
# prefixes; label + blurb are what the Preferences dialog shows.
# Order here is the DEFAULT auto-fetch order (word-sync sources
# first, line-only last) — the user can reorder it in Preferences.
PROVIDERS = [
    ("betterlyrics", "Better Lyrics", "TTML, syllable sync (cache-first)"),
    ("unison", "Unison", "Community uploads, TTML/LRC"),
    ("amll", "AMLL", "Hand-timed TTML, small corpus"),
    ("qq", "QQ Music", "QRC, word sync"),
    ("netease", "NetEase", "yrc word sync / LRC"),
    ("lrclib", "LRCLIB", "LRC, line sync"),
    ("kugou", "Kugou", "LRC, line sync"),
]
DEFAULT_PROVIDER_ORDER = [key for key, _label, _blurb in PROVIDERS]


def normalise_provider_prefs(order, enabled):
    """Clean a (provider_order, provider_enabled) pair from config.

    Drops unknown keys, appends any known provider missing from the
    order (at the end, in default order — so a new provider added by
    an update is on, not silently absent), and defaults every
    provider to enabled unless the config says otherwise.
    Returns (order, enabled) with enabled as a full dict.
    """
    known = set(DEFAULT_PROVIDER_ORDER)
    clean_order = [k for k in (order or []) if k in known]
    for k in DEFAULT_PROVIDER_ORDER:
        if k not in clean_order:
            clean_order.append(k)
    clean_enabled = {}
    for k in DEFAULT_PROVIDER_ORDER:
        clean_enabled[k] = bool((enabled or {}).get(k, True))
    return clean_order, clean_enabled
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
    `*` (e.g. a multiplication sign) does not count. Punctuation
    attached to the token (`****,`) does not hide it: the check runs
    on the token's core, asterisks included. A track that
    legitimately uses `***` as a separator would false-positive;
    that is rare, and the only cost is a "censored" tag and a lower
    sort rank in the picker.
    """
    for token in re.split(r"\s+", text or ""):
        if not token or "*" not in token:
            continue
        _lead, core, _trail = _split_punct(token)
        if len(core) >= 2 and core.strip("*") == "":
            return True  # bare "****" standing in for a word
        if any(c.isalnum() for c in core):
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
    raw_kind: str = ""  # "ttml" | "lrc" | "qrc" | "yrc" | ""
    cache_file: str = ""  # cache file this result came from / was saved to
    # The raw LRCLIB record this result was built from (None for
    # Better Lyrics / demo). Kept so a picked search candidate can be
    # cached verbatim under the current track's key — the raw
    # syncedLyrics keeps its Enhanced-LRC word tags, which a rebuild
    # from parsed lines would lose.
    raw_record: dict | None = None
    # Set by decensor_result(): asterisked words were replaced from
    # an uncensored copy of the same track (see below). Timings are
    # still this result's own — only word text changed.
    decensored: bool = False
    decensored_from: str = ""  # donor provider source, e.g. "search"

    @property
    def has_synced(self) -> bool:
        return bool(self.synced_lines)

    @property
    def has_word_sync(self) -> bool:
        """True when at least one line carries per-word timings."""
        return any(line.words for line in self.synced_lines)

    @property
    def is_censored(self) -> bool:
        """True when the displayed lyrics carry asterisk-censored
        words (see `is_censored_text`). Normally checked against
        `raw_text`, which every provider sets. After de-censoring,
        the displayed text is the fixed lines, so those are checked
        instead — a fully fixed result reports uncensored, a
        partially fixed one still reports censored."""
        if self.decensored:
            return is_censored_text(
                " ".join(line.text for line in self.synced_lines))
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

    Elements are matched by local name, not namespace: some TTML
    (AMLL's) resets the default namespace inside <div> (`xmlns=""`),
    so its <p>/<span> elements are in no namespace at all, while
    Better Lyrics' are in the TTML namespace. Matching
    `{http://www.w3.org/ns/ttml}p` would miss the AMLL ones entirely.
    """
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    body = None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "body":
            body = el
            break
    if body is None:
        body = root
    lines: list[LyricLine] = []
    for p in body.iter():
        if p.tag.rsplit("}", 1)[-1] != "p":
            continue
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
            if (cur_text.strip() and span["text"][:1] in " \t\n"):
                # AMLL convention: the space starts the next word
                # (" beat") instead of ending the previous one
                # ("Look "). End the word being built first, so the
                # sweep highlights word by word, not in merged chunks.
                words.append(LyricWord(time=cur_begin, end=cur_end or cur_begin,
                                       text=cur_text.strip(),
                                       is_background=cur_bg,
                                       parts=cur_parts))
                cur_text, cur_begin, cur_end, cur_bg = "", None, None, False
                cur_parts = []
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
                if cur_text.strip():
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


# ------------------------------------------------------- match scoring ---
def _norm_text(s: str) -> str:
    """Lowercase, punctuation-folded text for fuzzy title/artist matching."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _match_score(title: str, artist: str,
                 cand_titles, cand_artists) -> int:
    """0-4 score for a search candidate: title exact 2 / contains 1,
    artist exact 2 / contains 1. Callers require >= 3 — a wrong song
    that shares the title must not win on the title alone."""
    t = _norm_text(title)
    a = _norm_text(artist)
    score = 0
    for ct in cand_titles or []:
        n = _norm_text(ct)
        if n and n == t:
            score += 2
            break
        if n and t and (n in t or t in n):
            score += 1
            break
    for ca in cand_artists or []:
        n = _norm_text(ca)
        if not n or not a:
            continue
        if n == a:
            score += 2
            break
        if n in a or a in n:
            score += 1
            break
    return score


def _get_json_url(url: str, headers: dict | None = None,
                  timeout: float = 8.0) -> dict | None:
    """GET a JSON payload. Returns the parsed dict, or None on any
    HTTP/network/parse failure (callers fall through — never fail
    the track for one provider). Every request is logged."""
    req = urllib.request.Request(
        url, headers={"User-Agent": CLIENT_HEADER,
                      "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
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


def _unison_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "un-" + _cache_key(artist, track, album, duration)


def _amll_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "am-" + _cache_key(artist, track, album, duration)


def _netease_cache_key(artist: str, track: str, album: str, duration: float | None) -> str:
    return "ne-" + _cache_key(artist, track, album, duration)


def fetch_unison(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch community lyrics from Unison (Better Lyrics' uploads).

    GET /lyrics?song=&artist=&album=&duration= — no key. The payload
    is {"success": bool, "data": {"lyrics": "...", "format":
    "ttml"|"lrc"}}; TTML is word-level, LRC is parsed like any LRC
    (Enhanced-LRC word tags included). Returns None on NOT_FOUND or
    any error.
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _unison_cache_key(artist, track, album, duration)

    def _from_raw(raw: str, fmt: str, source: str) -> LyricsResult | None:
        if not raw:
            return None
        if fmt == "ttml" or raw.lstrip().startswith("<"):
            lines = parse_ttml(raw)
            kind = "ttml"
        else:
            lines = parse_lrc(raw)
            kind = "lrc"
        if not lines:
            return None
        return LyricsResult(
            artist=artist, track=track, album=album, duration=duration,
            synced_lines=lines, source=source, raw_text=raw, raw_kind=kind,
            cache_file=str(_cache_path(cache, key)))

    if use_cache:
        try:
            p = _cache_path(cache, key)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                result = _from_raw(data.get("raw") or "",
                                   data.get("format") or "", "unison-cache")
                if result is not None:
                    return result
        except Exception:
            pass  # corrupt cache: fall through to network

    params = {"song": track, "artist": artist}
    if album:
        params["album"] = album
    if duration is not None:
        params["duration"] = str(int(round(duration)))
    url = f"{UNISON_BASE}/lyrics?{urllib.parse.urlencode(params)}"
    payload = _get_json_url(url)
    if payload is None or not payload.get("success"):
        return None
    data = payload.get("data") or {}
    raw = data.get("lyrics") or ""
    fmt = (data.get("format") or "").lower()
    result = _from_raw(raw, fmt, "unison")
    if result is None:
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _cache_path(cache, key).write_text(
            json.dumps({"raw": raw, "format": fmt, "fetched_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return result


def fetch_amll(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch hand-timed syllable TTML from the AMLL TTML Database.

    Two steps, no key: search returns metadata only
    (GET /v1/lyrics/search?musicName=&artistName=&pageSize=), the
    best-scoring entry is then fetched by id
    (GET /v1/lyrics/get?id=). The corpus is small and hand-timed,
    so a match here is high quality — but most tracks miss and fall
    through. A match needs score >= 3 (see `_match_score`).
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _amll_cache_key(artist, track, album, duration)

    def _from_ttml(ttml: str, source: str) -> LyricsResult | None:
        lines = parse_ttml(ttml)
        if not lines:
            return None
        return LyricsResult(
            artist=artist, track=track, album=album, duration=duration,
            synced_lines=lines, source=source, raw_text=ttml, raw_kind="ttml",
            cache_file=str(_cache_path(cache, key)))

    if use_cache:
        try:
            p = _cache_path(cache, key)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                result = _from_ttml(data.get("ttml") or "", "amll-cache")
                if result is not None:
                    return result
        except Exception:
            pass  # corrupt cache: fall through to network

    params = {"musicName": track, "pageSize": "10"}
    if artist:
        params["artistName"] = artist
    search = _get_json_url(
        f"{AMLL_BASE}/v1/lyrics/search?{urllib.parse.urlencode(params)}")
    if search is None:
        return None
    items = (search.get("data") or {}).get("items") or []
    best = None
    best_score = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        score = _match_score(track, artist,
                             item.get("musicNames") or [],
                             item.get("artistNames") or [])
        if score > best_score:
            best_score = score
            best = item
    if best is None or best_score < 3 or not best.get("id"):
        return None
    got = _get_json_url(f"{AMLL_BASE}/v1/lyrics/get?id={best['id']}")
    if got is None:
        return None
    ttml = (got.get("data") or {}).get("lyrics") or ""
    result = _from_ttml(ttml, "amll")
    if result is None:
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _cache_path(cache, key).write_text(
            json.dumps({"ttml": ttml, "amll_id": best.get("id"),
                        "fetched_at": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return result


def fetch_netease(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> LyricsResult | None:
    """Fetch lyrics from NetEase Cloud Music (unofficial endpoints).

    GET /api/search/get?s=&type=1&limit=&offset= (plain JSON, no key
    — the /get/web variant returns an encrypted blob, this one does
    not), then GET /api/song/lyric?id=&lv=-1&kv=-1&tv=-1. The `yrc`
    field is word-level (same shape as QRC, parsed with `parse_qrc`);
    when it is empty the line-level `lrc` is used. Some recordings
    have no lyrics at all ("暂无歌词"). A Referer header is sent
    (the web player's).
    """
    artist = (artist or "").strip()
    track = (track or "").strip()
    if not artist or not track:
        return None
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    key = _netease_cache_key(artist, track, album, duration)

    def _from_payload(yrc: str, lrc: str, source: str) -> LyricsResult | None:
        if yrc:
            lines = parse_qrc(yrc)
            if lines:
                return LyricsResult(
                    artist=artist, track=track, album=album, duration=duration,
                    synced_lines=lines, source=source, raw_text=yrc, raw_kind="yrc",
                    cache_file=str(_cache_path(cache, key)))
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
                result = _from_payload(data.get("yrc") or "",
                                       data.get("lrc") or "", "netease-cache")
                if result is not None:
                    return result
        except Exception:
            pass  # corrupt cache: fall through to network

    # Search: title + artist first, then title alone. Best score wins;
    # duration breaks ties (NetEase reports it in milliseconds).
    song_id = None
    for term in (f"{track} {artist}".strip(), track):
        params = {"s": term, "type": 1, "limit": 5, "offset": 0}
        found = _get_json_url(
            f"{NETEASE_BASE}/api/search/get?{urllib.parse.urlencode(params)}",
            headers={"Referer": "https://music.163.com/"})
        if found is None:
            continue
        songs = (found.get("result") or {}).get("songs") or []
        best = None
        best_key = None
        for song in songs:
            if not isinstance(song, dict) or not song.get("id"):
                continue
            score = _match_score(
                track, artist, [song.get("name") or ""],
                [a.get("name") or "" for a in (song.get("artists") or [])])
            if score < 3:
                continue
            dur = song.get("duration")
            diff = (abs(dur / 1000.0 - duration)
                    if isinstance(dur, (int, float)) and duration else 9999.0)
            cand_key = (score, -diff)
            if best_key is None or cand_key > best_key:
                best_key = cand_key
                best = song
        if best is not None:
            song_id = best["id"]
            break
    if song_id is None:
        return None

    payload = _get_json_url(
        f"{NETEASE_BASE}/api/song/lyric?id={song_id}&lv=-1&kv=-1&tv=-1",
        headers={"Referer": "https://music.163.com/"})
    if payload is None:
        return None
    yrc = ((payload.get("yrc") or {}).get("lyric") or "")
    lrc = ((payload.get("lrc") or {}).get("lyric") or "")
    if "暂无歌词" in lrc and not yrc:
        return None
    result = _from_payload(yrc, lrc, "netease")
    if result is None:
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _cache_path(cache, key).write_text(
            json.dumps({"yrc": yrc, "lrc": lrc, "fetched_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return result


# The auto-fetch chain, as one function so the order is defined in
# exactly one place (worker, tests, picker all go through it).
# Built lazily: fetch_betterlyrics / fetch_lrclib are defined below
# this block, so a module-level dict would NameError at import.
def _fetcher_map() -> dict:
    return {
        "betterlyrics": fetch_betterlyrics,
        "unison": fetch_unison,
        "amll": fetch_amll,
        "qq": fetch_qq,
        "netease": fetch_netease,
        "lrclib": fetch_lrclib,
        "kugou": fetch_kugou,
    }


# Providers that can return word-synced lyrics at all. Kugou only
# ever returns line-level LRC, so once every provider left to try is
# line-only the hunt for word sync is over.
WORD_CAPABLE = {"betterlyrics", "unison", "amll", "qq", "netease", "lrclib"}


def _split_punct(word: str) -> tuple[str, str, str]:
    """(leading punctuation, core, trailing punctuation) of a word.

    `*` counts as part of the core, not punctuation — it is the
    censor marker, and the match below needs to see it.
    """
    start = 0
    while start < len(word) and not word[start].isalnum() and word[start] != "*":
        start += 1
    end = len(word)
    while end > start and not word[end - 1].isalnum() and word[end - 1] != "*":
        end -= 1
    return word[:start], word[start:end], word[end:]


def _censored_word_match(censored: str, donor: str) -> bool:
    """True when `donor` is the uncensored form of asterisked `censored`.

    Same length, and every non-`*` character matches at the same
    position (case-insensitive): `f**k` matches `fuck`, `Sh*t`
    matches `shit`, `ni**a` matches `nigga` — but `f**k` does NOT
    match `duck` (first letter differs). A fully-asterisked word
    (`****`) has no letters to verify against, so it matches any
    uncensored donor word — the line-level match (same word count,
    every other word identical, timestamp within 1.0 s) is the proof
    there, and the caller only calls this for a donor line already
    proven to be the same line. A donor word that is itself
    asterisked never matches.
    """
    _, c_core, _ = _split_punct(censored)
    _, d_core, _ = _split_punct(donor)
    if "*" not in c_core or "*" in d_core:
        return False
    if c_core.strip("*") == "":
        return True  # fully asterisked: line match is the proof
    if len(c_core) != len(d_core):
        return False
    return all(c == "*" or c.lower() == d.lower()
               for c, d in zip(c_core, d_core))


def _donor_words(donor_line: LyricLine) -> list[str]:
    """The donor line's words as plain strings — timed words when it
    has them, otherwise the line text split on whitespace."""
    if donor_line.words:
        return [w.text for w in donor_line.words]
    return donor_line.text.split()


def _align_lines(censored: LyricLine, donor: LyricLine) -> dict[int, int] | None:
    """Align a censored line to a donor line; `{winner_idx: donor_idx}`
    when the donor line is provably the same line, else None.

    Different copies of the same song routinely disagree on a word
    or two — one has "like" where the other doesn't, one hears
    "shashasha" where the other hears "chachacha", "girly" vs
    "girlie" — so a strict same-word-count, same-position check
    refuses donors that are plainly the same line (Y2K & bbno$ —
    Lalala, 2026-11-16: NetEase's "I got your b***h singing like
    lalala, lalala" against LRCLIB's "I got your bitch singing
    lalalala, lalala"). The alignment is therefore done with
    difflib over the words' punctuation-stripped cores, and the
    proof is the anchors, not the count:

    - every non-censored winner word must land in an `equal` block
      (identical, in order, in the donor) — at least one anchor
      word is required, so a line of only asterisks never matches
      here (it is proven by its neighbours instead);
    - a censored word must land in a 1:1 `replace` block, so there
      is exactly one donor word to take — a censored word in a
      `delete` block (the donor drops it entirely) or in a
      many-to-one `replace` refuses the line;
    - `delete` / `insert` blocks of uncensored words are tolerated:
      they are transcription variants, and the caller never
      substitutes them — only asterisked words are ever replaced,
      and each one still has to pass `_censored_word_match`
      letter-for-letter around the asterisks.
    """
    c_words = ([w.text for w in censored.words] if censored.words
               else censored.text.split())
    d_words = _donor_words(donor)
    if not c_words or not d_words:
        return None
    c_cores = [_split_punct(w)[1].lower() for w in c_words]
    d_cores = [_split_punct(w)[1].lower() for w in d_words]
    matcher = difflib.SequenceMatcher(a=c_cores, b=d_cores, autojunk=False)
    mapping: dict[int, int] = {}
    anchors = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[i1 + k] = j1 + k
            anchors += i2 - i1
        elif tag == "replace":
            if i2 - i1 != j2 - j1:
                # Ambiguous: several words collapse into fewer (or
                # vice versa) — refuse if a censored word is in
                # there, tolerate a pure transcription variant.
                if any("*" in w for w in c_words[i1:i2]):
                    return None
            else:
                for k in range(i2 - i1):
                    mapping[i1 + k] = j1 + k
        elif tag == "delete":
            # Winner-only words. A censored word with no donor
            # counterpart has nothing to take — refuse the line.
            if any("*" in w for w in c_words[i1:i2]):
                return None
        # "insert": donor-only words, tolerated (transcription
        # variant); nothing to map.
    return mapping if anchors >= 1 else None


def _lines_compatible(censored: LyricLine, donor: LyricLine) -> bool:
    """True when the donor line is provably the same line — see
    `_align_lines`. At least one anchor word is required; a line of
    only asterisked words cannot be verified by itself and never
    matches here; `decensor_result()` can still prove such a line
    from its neighbours (see `_neighbour_anchored`)."""
    return _align_lines(censored, donor) is not None


def _is_all_censored(line: LyricLine) -> bool:
    """True when every word in the line is asterisk-censored."""
    words = ([w.text for w in line.words] if line.words
             else line.text.split())
    return bool(words) and all("*" in w for w in words)


def _has_anchor_word(line: LyricLine) -> bool:
    """True when the line has at least one uncensored word."""
    words = ([w.text for w in line.words] if line.words
             else line.text.split())
    return any("*" not in w for w in words)


def _neighbour_anchored(result: LyricsResult, donor: LyricsResult,
                        idx: int) -> bool:
    """True when a fully-asterisked line at `idx` is proven by its
    neighbours: the donor line at the same position (same line
    number) is the candidate, and at least one adjacent line that
    exists in both copies provably matches at that same position —
    while no adjacent anchored line contradicts it (an anchored
    neighbour that does NOT match the donor at the same position
    means the two copies' segmentation has shifted, so refuse).
    An all-censored neighbour neither confirms nor contradicts.
    The caller also checks the candidate's time (within 1.0 s) and
    word count, so the proof is: same position, same timing, same
    length, flanked by the same lyrics."""
    confirmed = False
    for j in (idx - 1, idx + 1):
        if not (0 <= j < len(result.synced_lines)
                and 0 <= j < len(donor.synced_lines)):
            continue
        neighbour = result.synced_lines[j]
        d_neighbour = donor.synced_lines[j]
        if _lines_compatible(neighbour, d_neighbour):
            confirmed = True
        elif _has_anchor_word(neighbour):
            return False
    return confirmed


def decensor_result(result: LyricsResult,
                    donors: list[LyricsResult]) -> LyricsResult | None:
    """Return a copy of `result` with asterisked words replaced from
    an uncensored donor copy of the same track — or None when nothing
    could be fixed.

    The first uncensored donor with synced lines wins (a different
    provider than the result itself — the same source would carry the
    same asterisks). For each censored line, the nearest donor line
    in time (within 1.0 s) that is provably the same line lends its
    words; a word is substituted only when it matches the asterisked
    token letter-for-letter around the asterisks — or, when the token
    is fully asterisked (`****`), when the rest of the line anchors
    it. "Provably the same line" is an alignment, not an exact
    match: the donor may drop or add an uncensored word ("like"),
    or hear a word differently ("girly" / "girlie", "shashasha" /
    "chachacha") — see `_align_lines`. A line of only asterisks has
    no anchor words of its own, so it is proven instead by its
    neighbours: the donor line at the same line number, within 1.0 s,
    with the same word count, and at least one adjacent line
    provably matching at the same position (see
    `_neighbour_anchored`). A line with no matching neighbour
    — a lone `****` line, or a track that is all asterisks — is
    still left alone rather than guessed. Timings ALWAYS stay
    the censored result's own — only word text changes, and a fixed
    word's syllable parts collapse to the whole word (the split is
    lost, the word itself is correct). The copy is flagged
    `decensored` with `decensored_from` naming the donor provider;
    `raw_text` keeps the original censored file for the debug view.
    """
    if not result.is_censored or not result.has_synced:
        return None
    donor = None
    for cand in donors:
        if cand is result or cand.source == result.source:
            continue
        if cand.is_censored or not cand.has_synced:
            continue
        donor = cand
        break
    if donor is None:
        return None
    # First pass: lines provable on their own (anchor words in the
    # line itself). Record the best donor line per censored line,
    # with its word alignment — the donor may have a different
    # word count, so substitutions follow the alignment, not the
    # position.
    best_lines: list[tuple[LyricLine, dict[int, int]] | None] = []
    for line in result.synced_lines:
        best: tuple[LyricLine, dict[int, int]] | None = None
        best_dt = 1.0
        for dline in donor.synced_lines:
            dt = abs(dline.time - line.time)
            if dt > best_dt:
                continue
            mapping = _align_lines(line, dline)
            if mapping is not None:
                best = (dline, mapping)
                best_dt = dt
        best_lines.append(best)
    # Second pass: lines of only asterisks, proven by neighbours.
    for i, line in enumerate(result.synced_lines):
        if best_lines[i] is not None or not _is_all_censored(line):
            continue
        if i >= len(donor.synced_lines):
            continue
        dline = donor.synced_lines[i]
        if abs(dline.time - line.time) > 1.0:
            continue
        c_words = ([w.text for w in line.words] if line.words
                   else line.text.split())
        if len(c_words) != len(_donor_words(dline)) or not c_words:
            continue
        if _neighbour_anchored(result, donor, i):
            # Same word count here, so the alignment is positional.
            best_lines[i] = (dline, {k: k for k in range(len(c_words))})
    fixed_lines: list[LyricLine] = []
    fixed_any = False
    for line, best in zip(result.synced_lines, best_lines):
        if best is None:
            fixed_lines.append(line)
            continue
        best_line, mapping = best
        d_words = _donor_words(best_line)
        c_words = ([w.text for w in line.words] if line.words
                   else line.text.split())
        new_text = line.text
        new_words: list[LyricWord] = []
        for idx, c in enumerate(c_words):
            d = d_words[mapping[idx]] if idx in mapping else None
            if d is not None and "*" in c and _censored_word_match(c, d):
                lead, _core, trail = _split_punct(c)
                _d_lead, d_core, _d_trail = _split_punct(d)
                fixed = lead + d_core + trail
                if fixed != c:
                    new_text = new_text.replace(c, fixed, 1)
                    fixed_any = True
            else:
                fixed = c
            if line.words:
                w = line.words[idx]
                parts = ([LyricWord(w.time, w.end, fixed)]
                         if w.parts else [])
                new_words.append(LyricWord(w.time, w.end, fixed,
                                           w.is_background, parts=parts))
        fixed_lines.append(LyricLine(line.time, new_text, words=new_words))
    if not fixed_any:
        return None
    from dataclasses import replace as _replace
    return _replace(result, synced_lines=fixed_lines, decensored=True,
                    decensored_from=donor.source)


def _maybe_decensor(result, artist, track, album, duration, order, enabled,
                    decensor: bool):
    """De-censor a winning auto-fetch result from the other providers'
    copies of the same track, when enabled and when it is censored.
    The donor pool comes from `search_all_candidates` (cached after
    the first fetch, so repeat plays are local reads). Returns the
    original result unchanged when there is nothing to fix."""
    if not decensor or result is None or not result.is_censored:
        return result
    try:
        donors = search_all_candidates(artist, track, album, duration,
                                       order=order, enabled=enabled)
    except Exception:
        return result
    fixed = decensor_result(result, donors)
    return fixed if fixed is not None else result


def fetch_lyrics(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    order: list | None = None,
    enabled: dict | None = None,
    word_only: bool = False,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    decensor: bool = True,
) -> LyricsResult | None:
    """First matching lyrics, trying providers in `order`.

    Defaults to the registry order. Disabled providers are skipped.
    Word/syllable sync is preferred over provider order: the first
    result WITH word sync wins, even when an earlier provider already
    returned line-only lyrics (e.g. Unison's line-only LRC must not
    beat AMLL's word-synced TTML, and NetEase's line fallback must
    not beat LRCLIB's word-tagged record). Only when no provider has
    word sync does the first line-synced (or plain) result win.
    With `word_only`, a line-only result is skipped and the chain
    continues — the caller gets word-synced lyrics or None, never a
    line-only fallback. A provider returning None just falls through
    to the next; the track never fails for one.
    """
    order, enabled = normalise_provider_prefs(order, enabled)
    fetchers = _fetcher_map()
    first_synced = None
    first_any = None
    for i, key in enumerate(order):
        if not enabled.get(key, True):
            continue
        fn = fetchers[key]
        try:
            result = fn(artist, track, album, duration,
                        cache_dir=cache_dir, use_cache=use_cache)
        except Exception:
            result = None
        if result is None:
            continue
        if word_only and not result.has_word_sync:
            continue
        if not (result.has_synced or result.plain_lyrics or result.instrumental):
            continue
        if result.has_word_sync:
            return _maybe_decensor(result, artist, track, album, duration,
                                   order, enabled, decensor)
        if first_synced is None and result.has_synced:
            first_synced = result
        if first_any is None:
            first_any = result
        # Keep looking while a later provider could still return
        # something better: word sync when we only have line sync
        # (or plain), anything synced when we only have plain.
        remaining = [k for k in order[i + 1:] if enabled.get(k, True)]
        if not remaining:
            break
        if first_synced is not None and not any(
                k in WORD_CAPABLE for k in remaining):
            break
    winner = first_synced if first_synced is not None else first_any
    if winner is None:
        return None
    return _maybe_decensor(winner, artist, track, album, duration,
                           order, enabled, decensor)


def search_all_candidates(
    artist: str,
    track: str,
    album: str | None = None,
    duration: float | None = None,
    order: list | None = None,
    enabled: dict | None = None,
    word_only: bool = False,
) -> list[LyricsResult]:
    """All lyric candidates for the picker, word-synced first.

    Every provider in `order` (except LRCLIB, which contributes its
    search results) + LRCLIB search results. Each provider is
    best-effort: one returning None just contributes nothing. Sorted:
    word-synced first, then synced, then plain-only — uncensored
    above censored within each rank, provider order within that.
    With `word_only`, line-only results are left out entirely.
    """
    order, enabled = normalise_provider_prefs(order, enabled)
    fetchers = _fetcher_map()
    candidates: list[LyricsResult] = []
    for key in order:
        if key == "lrclib" or not enabled.get(key, True):
            continue
        try:
            result = fetchers[key](artist, track, album, duration)
        except Exception:
            result = None
        if result is not None and (result.has_synced or result.plain_lyrics
                                   or result.instrumental):
            if word_only and not result.has_word_sync:
                continue
            candidates.append(result)
    if enabled.get("lrclib", True):
        try:
            for result in search_lrclib(artist, track, album, duration):
                if word_only and not result.has_word_sync:
                    continue
                candidates.append(result)
        except Exception:
            pass

    def _provider_rank(r: LyricsResult) -> int:
        for i, key in enumerate(order):
            if key == "lrclib":
                if r.source == "search" or r.source.startswith("lrclib"):
                    return i
            elif r.source.startswith(key):
                return i
        return len(order)

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
    """Delete all cache files for a track (Better Lyrics TTML +
    Unison + AMLL + QQ + NetEase + Kugou + LRCLIB).

    Used by a forced re-pull: the cached lyrics were wrong, so they
    must not survive a failed re-fetch either. Returns how many files
    were removed.
    """
    album = (album or "").strip()
    cache = cache_dir or DEFAULT_CACHE_DIR
    removed = 0
    for key in (_betterlyrics_cache_key(artist, track, album, duration),
                _unison_cache_key(artist, track, album, duration),
                _amll_cache_key(artist, track, album, duration),
                _qq_cache_key(artist, track, album, duration),
                _netease_cache_key(artist, track, album, duration),
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
