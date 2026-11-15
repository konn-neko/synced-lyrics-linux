"""Shared playback state: ONE MPRIS poller, ONE lyrics fetcher.

Both windows (Full App and Overlay) and the tray read from this single
PlaybackEngine -- there is never a second poller, so switching modes
never loses sync. The engine owns:
  * the selected MPRIS player (or demo mode)
  * position polling every 250 ms, interpolated with a monotonic clock
    (browsers report position in 1 s steps, so interpolation matters)
  * the current LyricsResult, fetched in a worker thread — Better
    Lyrics TTML (word sync) first, LRCLIB line sync as fallback
  * sync offset (seconds, added to the position before line lookup)
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

import mpris
from lyrics import (
    DEFAULT_PROVIDER_ORDER,
    LRCLIBRateLimited,
    LyricsResult,
    cache_lrclib_record,
    clear_track_cache,
    demo_result,
    fetch_lyrics,
    normalise_provider_prefs,
    search_all_candidates,
)


class MonotonicPosition:
    """Interpolated position that never goes backwards.

    Browsers (YouTube Music in Firefox/Chromium) report MPRIS Position
    in 1 s steps, so the reported value stays flat for ~4 polls and then
    jumps a whole second. Naively resetting the interpolation anchor to
    each report makes the estimate sawtooth back to the floored value
    on every poll — and if a line boundary falls inside that window,
    line_at() flips between two lines on every poll and the scroll
    animation gets retargeted up and down forever.

    Instead, keep an anchor (position + monotonic time it was set) and
    interpolate from it while Playing. A new report is adopted as the
    anchor when it is ahead of the estimate minus a small tolerance
    (normal advance / jitter), or when it is far away either way
    (a real seek). A report behind the estimate by more than the
    tolerance is a stale/floored report and is ignored, so the estimate
    never goes backwards.
    """

    STALE_TOLERANCE = 0.5  # s: ignore reports this far behind the estimate
    SEEK_JUMP = 2.0        # s: a jump this big either way is a real seek

    def __init__(self):
        self.anchor = 0.0
        self.anchor_time: float | None = None

    def estimate(self, now: float, playing: bool = True) -> float:
        if self.anchor_time is None or not playing:
            return self.anchor
        return self.anchor + (now - self.anchor_time)

    def observe(self, reported: float, now: float, playing: bool = True) -> float:
        est = self.estimate(now, playing)
        if reported >= est - self.STALE_TOLERANCE or abs(reported - est) > self.SEEK_JUMP:
            self.anchor = float(reported)
            self.anchor_time = now
        return self.estimate(now, playing)

    def set(self, position: float, now: float) -> None:
        self.anchor = float(position)
        self.anchor_time = now


class _LyricsWorker(QObject):
    result_ready = Signal(object)  # (key, LyricsResult | None | ("error", str))
    search_ready = Signal(object)  # (key, [LyricsResult] | ("error", str))
    fetch_requested = Signal(str, str, str, float, object, bool)
    search_requested = Signal(str, str, str, float, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Lyrics preferences, set by the engine (which gets them from
        # the app config / Preferences dialog). The worker reads them
        # when a queued fetch/search slot runs, so a prefs change
        # applies to the next lookup without restarting anything.
        # (order, enabled dict, word_only) — defaults: registry
        # order, everything on, word sync preferred but not required.
        self.lyrics_prefs = (list(DEFAULT_PROVIDER_ORDER), {}, False)
        self.fetch_requested.connect(self.fetch)
        self.search_requested.connect(self.search)

    def fetch(self, artist, track, album, duration, key, force=False):
        try:
            if force:
                # The cached lyrics were wrong: drop them first so a
                # failed re-pull can't leave the wrong ones in place.
                clear_track_cache(artist, track, album, duration)
            order, enabled, word_only = self.lyrics_prefs
            result = fetch_lyrics(artist, track, album, duration,
                                  order=order, enabled=enabled,
                                  word_only=word_only, use_cache=not force)
            self.result_ready.emit((key, result))
        except LRCLIBRateLimited as e:
            self.result_ready.emit((key, ("error", f"LRCLIB rate limited — retry in {e.retry_after:.0f}s")))
        except Exception as e:  # network etc: surface, don't crash the poller
            self.result_ready.emit((key, ("error", str(e))))

    def search(self, artist, track, album, duration, key):
        """All lyric candidates for the picker (all providers)."""
        try:
            order, enabled, word_only = self.lyrics_prefs
            results = search_all_candidates(artist, track, album, duration,
                                            order=order, enabled=enabled,
                                            word_only=word_only)
            self.search_ready.emit((key, results))
        except LRCLIBRateLimited as e:
            self.search_ready.emit((key, ("error", f"LRCLIB rate limited — retry in {e.retry_after:.0f}s")))
        except Exception as e:
            self.search_ready.emit((key, ("error", str(e))))


class PlaybackEngine(QObject):
    track_changed = Signal(object)      # mpris.TrackInfo
    lyrics_changed = Signal(object)     # LyricsResult | None
    candidates_ready = Signal(object)   # (key, [LyricsResult] | ("error", str))
    status_changed = Signal(str)        # human-readable status line
    position_changed = Signal(float, int)  # position seconds, current line index
    players_changed = Signal(list)      # list[str]

    POLL_MS = 250

    def __init__(self, demo: bool = False, parent=None):
        super().__init__(parent)
        self.demo = demo
        self.player: str = ""
        self.track = mpris.TrackInfo()
        self.lyrics: LyricsResult | None = None
        self.offset: float = 0.0
        self._pos = MonotonicPosition()
        self._emit_floor: float | None = None
        self._last_poll = time.monotonic()
        self._demo_start = time.monotonic()
        self._demo_pos = 0.0
        self._demo_playing = True
        self._fetch_key: tuple | None = None
        self._search_key: tuple | None = None
        self._players: list[str] = []
        # Lyrics preferences (provider order / enabled / word-only),
        # from the app config via set_lyrics_prefs. Defaults match
        # the registry until the controller hands over the real ones.
        self.provider_order = list(DEFAULT_PROVIDER_ORDER)
        self.provider_enabled = {k: True for k in DEFAULT_PROVIDER_ORDER}
        self.word_only = False

        self._worker_thread = QThread(self)
        self._worker = _LyricsWorker()
        self._worker.moveToThread(self._worker_thread)
        # fetch_requested is emitted from the GUI thread and queued to the worker
        self._fetch_requested = self._worker.fetch_requested
        self._worker.result_ready.connect(self._on_lyrics_result)
        self._worker.search_ready.connect(self._on_search_result)
        self._worker_thread.start()

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        self._players_timer = QTimer(self)
        self._players_timer.setInterval(2000)
        self._players_timer.timeout.connect(self._refresh_players)
        self._players_timer.start()

        if demo:
            self._load_demo()
        else:
            self._refresh_players()

    # ------------------------------------------------------------ lifecycle
    def stop(self):
        self._timer.stop()
        self._players_timer.stop()
        self._worker_thread.quit()
        self._worker_thread.wait(2000)

    def set_player(self, player: str):
        if player != self.player:
            self.player = player
            self.track = mpris.TrackInfo(player=player)
            self.lyrics = None
            self._fetch_key = None
            self._emit_floor = None
            self.lyrics_changed.emit(None)
            self.poll()

    def set_offset(self, seconds: float):
        self.offset = seconds
        self._emit_position()

    def set_lyrics_prefs(self, order, enabled, word_only: bool):
        """Apply the lyrics preferences (from the Preferences dialog).

        Normalised here so a stale config can never leave a provider
        out of the chain. Re-fetches the current track: the displayed
        lyrics may have come from a provider that is now off, or may
        be line-only under a new word-only rule.
        """
        order, enabled = normalise_provider_prefs(order, enabled)
        self.provider_order = order
        self.provider_enabled = enabled
        self.word_only = bool(word_only)
        self._worker.lyrics_prefs = (order, enabled, bool(word_only))
        if self.track.title and self.track.artist and not self.demo:
            self._fetch_lyrics(self.track)

    # -------------------------------------------------------------- polling
    def _refresh_players(self):
        if self.demo:
            return
        players = mpris.list_players()
        if players != self._players:
            self._players = players
            self.players_changed.emit(list(players))
            if not self.player and players:
                self.player = players[0]
            elif self.player and self.player not in players:
                self.player = players[0] if players else ""

    def _load_demo(self):
        from lyrics import DEMO_ALBUM, DEMO_ARTIST, DEMO_DURATION, DEMO_TRACK
        self.track = mpris.TrackInfo(
            player="demo", title=DEMO_TRACK, artist=DEMO_ARTIST,
            album=DEMO_ALBUM, duration=DEMO_DURATION,
            position=0.0, status="Playing")
        self._demo_start = time.monotonic()
        self._demo_pos = 0.0
        self.lyrics = demo_result()
        self.track_changed.emit(self.track)
        self.lyrics_changed.emit(self.lyrics)
        self.status_changed.emit("Demo mode — Coldplay — Yellow (sample lyrics)")

    def poll(self):
        now = time.monotonic()
        if self.demo:
            if self._demo_playing:
                self._demo_pos = min(
                    self.track.duration,
                    self._demo_pos + (now - self._last_poll))
                if self._demo_pos >= self.track.duration:
                    self._demo_pos = 0.0  # loop the demo
            self.track.position = self._demo_pos
            self.track.status = "Playing" if self._demo_playing else "Paused"
            self._last_poll = now
            self._emit_position()
            return

        self._last_poll = now
        if not self.player:
            self._refresh_players()
            if not self.player:
                self.status_changed.emit("No MPRIS player found — start Spotify or YouTube Music")
                return
        track = mpris.get_track(self.player)
        if track is None:
            self.status_changed.emit(f"Player '{self.player}' stopped responding")
            return
        # Monotonic anchor: a floored browser report must not drag the
        # interpolated estimate backwards (see MonotonicPosition).
        self._pos.observe(track.position, now, playing=(track.status == "Playing"))
        changed = track.key != self.track.key
        self.track = track
        if changed:
            self.track_changed.emit(track)
            self._fetch_lyrics(track)
        self._emit_position()

    def _emit_position(self):
        if self.demo:
            pos = self._demo_pos
        else:
            pos = self._pos.estimate(
                time.monotonic(), playing=(self.track.status == "Playing"))
            # Belt-and-braces monotonicity: microsecond scheduling jitter
            # between observe() and estimate() can make consecutive
            # emissions differ by ~1e-5 s in either direction. Clamp to
            # the last emitted value so the line index can never flip
            # back within a track. Seeks / track changes reset the floor.
            if self._emit_floor is not None:
                pos = max(pos, self._emit_floor)
            self._emit_floor = pos
        idx = self.lyrics.line_at(pos, self.offset) if self.lyrics else -1
        self.position_changed.emit(pos, idx)

    # -------------------------------------------------------------- lyrics
    def _fetch_lyrics(self, track):
        self._emit_floor = None  # new track: positions may start lower
        if not track.title or not track.artist:
            self.lyrics = None
            self.lyrics_changed.emit(None)
            self.status_changed.emit("No track metadata — cannot look up lyrics")
            return
        # Clear the previous track's lyrics NOW, not when the fetch
        # lands: the old lines would stay on screen for seconds
        # otherwise (seven providers, network round-trips) while the
        # new song is already playing.
        self.lyrics = None
        self.lyrics_changed.emit(None)
        self._fetch_key = track.key
        self.status_changed.emit(f"Looking up lyrics for {track.display} …")
        # Run the network fetch on the worker thread (queued signal).
        self._worker.fetch_requested.emit(track.artist, track.title, track.album, track.duration or 0.0, track.key, False)

    def _on_lyrics_result(self, payload):
        key, result = payload
        if key != self._fetch_key:
            return  # stale result for a track we already left
        if isinstance(result, tuple) and result and result[0] == "error":
            self.lyrics = None
            self.lyrics_changed.emit(None)
            self.status_changed.emit(result[1])
            return
        self.lyrics = result
        self.lyrics_changed.emit(result)
        if result is None:
            n = sum(1 for v in self.provider_enabled.values() if v)
            if self.word_only:
                self.status_changed.emit(
                    f"No word-synced lyrics found for this track "
                    f"({n} provider{'s' if n != 1 else ''} checked, "
                    f"line-only results skipped by Preferences)")
            else:
                self.status_changed.emit(f"No lyrics found ({n} providers checked) for this track")
        elif result.instrumental:
            self.status_changed.emit("Instrumental — no lyrics (LRCLIB)")
        elif result.has_synced:
            if result.source.startswith("betterlyrics"):
                provider = "Better Lyrics"
            elif result.source.startswith("unison"):
                provider = "Unison"
            elif result.source.startswith("amll"):
                provider = "AMLL"
            elif result.source.startswith("qq"):
                provider = "QQ Music"
            elif result.source.startswith("netease"):
                provider = "NetEase"
            elif result.source.startswith("kugou"):
                provider = "Kugou"
            else:
                provider = "LRCLIB"
            sync = "Word-synced" if result.has_word_sync else "Synced"
            cached = " [cached]" if "cache" in result.source else ""
            self.status_changed.emit(
                f"{sync} lyrics from {provider} ({len(result.synced_lines)} lines){cached}")
        elif result.plain_lyrics:
            self.status_changed.emit("Plain lyrics only — no sync data")
        else:
            n = sum(1 for v in self.provider_enabled.values() if v)
            self.status_changed.emit(f"No lyrics found ({n} providers checked) for this track")
        self._emit_position()

    def current_position(self) -> float:
        """Interpolated position right now (demo or monotonic estimate).

        Used by the mid-word sweep timers in the windows: they read
        the estimate ~16x/s instead of waiting for the 250 ms poll.
        This reads state only — it never polls MPRIS.
        """
        if self.demo:
            return self._demo_pos
        return self._pos.estimate(
            time.monotonic(), playing=(self.track.status == "Playing"))

    def word_index_at(self, pos: float) -> tuple[int, int]:
        """(line_idx, word_idx) active at `pos` (offset applied).

        Word index is -1 when the line has no word timings. Uses the
        same 250 ms interpolated position as the line lookup — no
        faster polling is needed for word highlighting.
        """
        if not self.lyrics:
            return (-1, -1)
        line_idx = self.lyrics.line_at(pos, self.offset)
        if line_idx < 0:
            return (-1, -1)
        return (line_idx, self.lyrics.word_at(line_idx, pos, self.offset))

    def search_manual(self, artist: str, track: str):
        """Manual lookup from the search box: pretends this is the track."""
        key = (artist.lower().strip(), track.lower().strip(), "", 0)
        self._fetch_key = key
        # Same as a track change: drop the old lyrics at once, so the
        # previous track's lines don't linger while this lookup runs.
        self.lyrics = None
        self.lyrics_changed.emit(None)
        self.status_changed.emit(f"Looking up lyrics for {artist} — {track} …")
        self._worker.fetch_requested.emit(artist, track, "", 0.0, key, False)

    def search_lyrics_candidates(self):
        """Start an LRCLIB search for the current track (picker list)."""
        track = self.track
        if not track.title or not track.artist:
            self.status_changed.emit("No track metadata — cannot search for lyrics")
            return
        self._search_key = track.key
        self._worker.search_requested.emit(
            track.artist, track.title, track.album, track.duration or 0.0,
            track.key)

    def _on_search_result(self, payload):
        key, _results = payload
        if key != self._search_key:
            return  # stale search for a track we already left
        self.candidates_ready.emit(payload)

    def refetch_lyrics(self):
        """Re-pull lyrics for the current track, ignoring the cache.

        Drops both cache files first, then fetches fresh (Better
        Lyrics, then LRCLIB). Demo mode has no network to re-pull from.
        """
        if self.demo:
            self.status_changed.emit("Demo mode — nothing to re-fetch")
            return
        track = self.track
        if not track.title or not track.artist:
            self.status_changed.emit("No track metadata — nothing to re-fetch")
            return
        self._fetch_key = track.key
        # The cached lyrics are known wrong — clear them at once rather
        # than showing them while the fresh pull runs.
        self.lyrics = None
        self.lyrics_changed.emit(None)
        self.status_changed.emit(
            f"Re-fetching lyrics for {track.display} (ignoring cache) …")
        self._worker.fetch_requested.emit(
            track.artist, track.title, track.album, track.duration or 0.0,
            track.key, True)

    def apply_candidate(self, result):
        """Use a lyrics result picked from the search list.

        The pick is cached verbatim under the current track's key, so
        the next lookup for this track loads it directly instead of
        re-picking the wrong match.
        """
        if result is None:
            return
        track = self.track
        if result.raw_record is not None and track.title and track.artist:
            path = cache_lrclib_record(
                result.raw_record, track.artist, track.title,
                track.album, track.duration or 0.0)
            if path:
                result.cache_file = path
        self.lyrics = result
        self.lyrics_changed.emit(result)
        flags = ("word-synced" if result.has_word_sync else
                 "synced" if result.has_synced else
                 "plain" if result.plain_lyrics else "no lyrics")
        if result.source.startswith("betterlyrics"):
            provider = "Better Lyrics"
        elif result.source.startswith("unison"):
            provider = "Unison"
        elif result.source.startswith("amll"):
            provider = "AMLL"
        elif result.source.startswith("qq"):
            provider = "QQ Music"
        elif result.source.startswith("netease"):
            provider = "NetEase"
        elif result.source.startswith("kugou"):
            provider = "Kugou"
        else:
            provider = "LRCLIB"
        self.status_changed.emit(
            f"Using lyrics: {result.artist} — {result.track}"
            + (f" ({result.album})" if result.album else "")
            + f" — {provider}, {flags}")
        self._emit_position()

    # ------------------------------------------------------------- controls
    def play_pause(self):
        if self.demo:
            self._demo_playing = not self._demo_playing
            self._emit_position()
            return
        if self.player:
            mpris.play_pause(self.player)

    def next_track(self):
        if self.demo:
            self._demo_pos = 0.0
            return
        if self.player:
            mpris.next_track(self.player)

    def previous_track(self):
        if self.demo:
            self._demo_pos = 0.0
            return
        if self.player:
            mpris.previous_track(self.player)

    def seek(self, seconds: float):
        if self.demo:
            self._demo_pos = max(0.0, min(self.track.duration, seconds))
            self._emit_position()
            return
        if self.player:
            mpris.seek(self.player, seconds)
            self._pos.set(seconds, time.monotonic())
            self._emit_floor = None  # a seek may legitimately go backwards
            self._emit_position()

    @property
    def current_line_index(self) -> int:
        if not self.lyrics:
            return -1
        pos = self._demo_pos if self.demo else self._pos.estimate(
            time.monotonic(), playing=(self.track.status == "Playing"))
        return self.lyrics.line_at(pos, self.offset)

    @property
    def current_line_text(self) -> str:
        if not self.lyrics or not self.lyrics.synced_lines:
            return ""
        idx = self.current_line_index
        return self.lyrics.synced_lines[idx].text if idx >= 0 else ""
