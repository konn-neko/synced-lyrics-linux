"""LRCLIB client + LRC parser for synced-lyrics-linux.

No API key, no login. LRCLIB is free: https://lrclib.net
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
from dataclasses import dataclass, field
from pathlib import Path

LRCLIB_BASE = "https://lrclib.net"
CLIENT_HEADER = "synced-lyrics-linux/1.0"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "synced-lyrics-linux"

_TAG_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
_WORD_TAG_RE = re.compile(r"<\d{1,2}:\d{2}(?:[.:]\d{1,3})?>")
_META_TAG_RE = re.compile(r"\[(ti|ar|al|by|length|#):[^\]]*\]", re.IGNORECASE)


@dataclass
class LyricLine:
    time: float  # seconds
    text: str


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


class LRCLIBRateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"LRCLIB rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def parse_lrc(text: str) -> list[LyricLine]:
    """Parse LRC text into sorted [LyricLine].

    Handles: [mm:ss.xx], [mm:ss], multiple timestamps per line,
    [offset:+/-ms], metadata tags ([ti:], [ar:] ... ignored),
    enhanced-LRC word tags <mm:ss.xx> (stripped, line text kept),
    empty / blank lines (skipped).
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
        # Find the end of the last [...] tag.
        last_end = 0
        for mm in _TAG_RE.finditer(line):
            last_end = mm.end()
        body = line[last_end:].strip()
        body = _WORD_TAG_RE.sub("", body).strip()
        body = re.sub(r"\s+", " ", body)
        body = _META_TAG_RE.sub("", body).strip()
        if not body:
            continue
        for minute, second, frac in tags:
            total = int(minute) * 60 + int(second)
            if frac:
                # .xx = hundredths, .x = tenths, .xxx = milliseconds
                if len(frac) == 1:
                    total += int(frac) / 10.0
                elif len(frac) == 2:
                    total += int(frac) / 100.0
                else:
                    total += int(frac) / 1000.0
            total += offset_ms / 1000.0
            if total < 0:
                total = 0.0
            lines.append(LyricLine(time=total, text=body))
    lines.sort(key=lambda l: l.time)
    return lines


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


def demo_result() -> LyricsResult:
    return LyricsResult(
        artist=DEMO_ARTIST,
        track=DEMO_TRACK,
        album=DEMO_ALBUM,
        duration=DEMO_DURATION,
        synced_lines=parse_lrc(DEMO_LRC),
        plain_lyrics="",
        instrumental=False,
        source="demo",
    )
