"""Shared playback state: ONE MPRIS poller, ONE lyrics fetcher.

Both windows (Full App and Overlay) and the tray read from this single
PlaybackEngine -- there is never a second poller, so switching modes
never loses sync. The engine owns:
  * the selected MPRIS player (or demo mode)
  * position polling every 250 ms, interpolated with a monotonic clock
    (browsers report position in 1 s steps, so interpolation matters)
  * the current LyricsResult, fetched in a worker thread from LRCLIB
  * sync offset (seconds, added to the position before line lookup)
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

import mpris
from lyrics import LRCLIBRateLimited, LyricsResult, demo_result, fetch_lrclib


class _LyricsWorker(QObject):
    result_ready = Signal(object)  # LyricsResult | None, or ("error", str)
    fetch_requested = Signal(str, str, str, float, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fetch_requested.connect(self.fetch)

    def fetch(self, artist, track, album, duration, key):
        try:
            result = fetch_lrclib(artist, track, album, duration)
            self.result_ready.emit((key, result))
        except LRCLIBRateLimited as e:
            self.result_ready.emit((key, ("error", f"LRCLIB rate limited — retry in {e.retry_after:.0f}s")))
        except Exception as e:  # network etc: surface, don't crash the poller
            self.result_ready.emit((key, ("error", str(e))))


class PlaybackEngine(QObject):
    track_changed = Signal(object)      # mpris.TrackInfo
    lyrics_changed = Signal(object)     # LyricsResult | None
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
        self._last_pos = 0.0
        self._last_poll = time.monotonic()
        self._demo_start = time.monotonic()
        self._demo_pos = 0.0
        self._demo_playing = True
        self._fetch_key: tuple | None = None
        self._players: list[str] = []

        self._worker_thread = QThread(self)
        self._worker = _LyricsWorker()
        self._worker.moveToThread(self._worker_thread)
        # fetch_requested is emitted from the GUI thread and queued to the worker
        self._fetch_requested = self._worker.fetch_requested
        self._worker.result_ready.connect(self._on_lyrics_result)
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
            self.lyrics_changed.emit(None)
            self.poll()

    def set_offset(self, seconds: float):
        self.offset = seconds
        self._emit_position()

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
        self._last_pos = track.position
        changed = track.key != self.track.key
        self.track = track
        if changed:
            self.track_changed.emit(track)
            self._fetch_lyrics(track)
        self._emit_position()

    def _emit_position(self):
        if self.demo:
            pos = self._demo_pos
        elif self.track.status == "Playing":
            pos = self._last_pos + (time.monotonic() - self._last_poll)
        else:
            pos = self._last_pos
        idx = self.lyrics.line_at(pos, self.offset) if self.lyrics else -1
        self.position_changed.emit(pos, idx)

    # -------------------------------------------------------------- lyrics
    def _fetch_lyrics(self, track):
        if not track.title or not track.artist:
            self.lyrics = None
            self.lyrics_changed.emit(None)
            self.status_changed.emit("No track metadata — cannot look up lyrics")
            return
        self._fetch_key = track.key
        self.status_changed.emit(f"Looking up lyrics for {track.display} …")
        # Run the network fetch on the worker thread (queued signal).
        self._worker.fetch_requested.emit(track.artist, track.title, track.album, track.duration or 0.0, track.key)

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
            self.status_changed.emit("No lyrics found on LRCLIB for this track")
        elif result.instrumental:
            self.status_changed.emit("Instrumental — no lyrics (LRCLIB)")
        elif result.has_synced:
            self.status_changed.emit(
                f"Synced lyrics from LRCLIB ({len(result.synced_lines)} lines)"
                + (" [cached]" if result.source == "cache" else ""))
        elif result.plain_lyrics:
            self.status_changed.emit("Plain lyrics only — no sync data (LRCLIB)")
        else:
            self.status_changed.emit("No lyrics found on LRCLIB for this track")
        self._emit_position()

    def search_manual(self, artist: str, track: str):
        """Manual lookup from the search box: pretends this is the track."""
        key = (artist.lower().strip(), track.lower().strip(), "", 0)
        self._fetch_key = key
        self.status_changed.emit(f"Looking up lyrics for {artist} — {track} …")
        self._worker.fetch_requested.emit(artist, track, "", 0.0, key)

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
            self._last_pos = seconds
            self._last_poll = time.monotonic()
            self._emit_position()

    @property
    def current_line_index(self) -> int:
        if not self.lyrics:
            return -1
        pos = self._demo_pos if self.demo else self._last_pos
        return self.lyrics.line_at(pos, self.offset)

    @property
    def current_line_text(self) -> str:
        if not self.lyrics or not self.lyrics.synced_lines:
            return ""
        idx = self.current_line_index
        return self.lyrics.synced_lines[idx].text if idx >= 0 else ""
