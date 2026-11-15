# Architecture — synced-lyrics-linux

Repo: https://github.com/konn-neko/synced-lyrics-linux

## Data flow

```
┌─────────────────────────────────┐
│  Player (Spotify, YouTube Music │
│  in Firefox/Chromium, VLC, …)   │
│  exposes MPRIS on the session   │
│  D-Bus: org.mpris.MediaPlayer2  │
└──────────────┬──────────────────┘
               │ Metadata, Position, PlaybackStatus
               │ (title, artist, album, duration, artUrl)
               ▼
┌──────────────────────────────────────────────────────────┐
│  PlaybackEngine  (playback.py) — ONE instance, ONE poller │
│                                                          │
│  • QTimer, 250 ms: get_track(player) via mpris.py        │
│  • Position interpolated with time.monotonic() between   │
│    polls (browsers report position in 1 s steps)         │
│  • Track change → fetch lyrics in a QThread worker       │
│  • Sync offset added at lookup time, not to the position │
│  • Signals: track_changed / lyrics_changed /             │
│    position_changed / players_changed / status_changed   │
└──────┬───────────────────────┬───────────────────────────┘
       │                       │ fetch (worker thread)
       ▼                       ▼
┌──────────────┐      ┌──────────────────────────────┐
│  mpris.py    │      │  LRCLIB  https://lrclib.net  │
│  QtDBus 1st, │      │  GET /api/get?… → /api/search│
│  playerctl   │      │  fallback; cache in          │
│  fallback    │      │  ~/.cache/synced-lyrics-linux│
└──────────────┘      └──────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  Renderers — all read the SAME engine state, none polls  │
├──────────────────┬───────────────────┬───────────────────┤
│  FullAppWindow   │  OverlayWindow    │  QSystemTrayIcon  │
│  (QMainWindow)   │  (frameless, top) │  (♪ icon, menu)   │
│  lyrics list,    │  current line     │  tooltip:         │
│  progress,       │  large + next     │  Artist — Title   │
│  controls,       │  line dimmed      │  + current line   │
│  search box      │                   │                   │
└──────────────────┴───────────────────┴───────────────────┘
```

### Sequence: a song starts

1. Engine poll (250 ms) calls `mpris.get_track(player)`.
2. `track.key` (artist/title/album/duration) differs from the previous
   track → `track_changed` emitted, lyrics fetch queued to the worker.
3. Worker calls `lyrics.fetch_lrclib()`: disk cache → `/api/get` →
   `/api/search` fallback. Result (or `None`, or an error string)
   comes back on `result_ready`.
4. Engine emits `lyrics_changed`; both windows rebuild their lyric
   lists; the status bar reports the source (`LRCLIB`, `[cached]`,
   `plain lyrics only`, `instrumental`, `no lyrics`).
5. Every poll, `position_changed(pos, line_index)` fires; each window
   highlights `line_index` (crimson, bold, larger in the Full App;
   large crimson in the Overlay) and scrolls/updates the next line.

### Sequence: user clicks a lyric line / seeks

Full App list item click (or progress-slider release) → `engine.seek(s)`
→ MPRIS seek via `playerctl position <s>` (or `SetPosition` fallback)
→ next poll picks up the new position → highlight jumps. In demo mode
the seek just moves the fake position.

## Mode switching

```
Full App ── "Pop out overlay" / Ctrl+2 / View menu ──► Overlay
   ▲                                                      │
   └────── double-click / menu / Ctrl+1 / tray ───────────┘
```

- Both windows exist for the life of the app; switching only
  shows/hides them. The engine keeps polling either way, so no sync
  is lost and playback is never restarted.
- `--mode full` (default) starts with the Full App visible;
  `--mode overlay` starts with the Overlay visible (and moved to the
  chosen display).
- Closing the Full App: if `minimise_to_tray` is on (default) and a
  tray icon exists, the window hides instead of quitting. Quit is via
  File → Quit or the tray menu. The Overlay's ✕ button only hides the
  Overlay.

## Display selection

```
QGuiApplication.screens()
  → "Display 1 — 1920x1080 (primary)", "Display 2 — …"
  → + "Primary display", "Follow mouse"
  → choice saved in ~/.config/synced-lyrics-linux/config.json
     as "display": "primary" | "follow" | <int index>
```

- Picking a display moves the Overlay immediately (if visible) to
  that screen's `availableGeometry()`, centred.
- The Full App does **not** jump on pick — it moves only via
  `View > Move Full App to selected display`, so it never relocates
  mid-use.
- A saved index that no longer exists (unplugged monitor) falls back
  to the primary screen. `--screen N` sets the choice for that launch.
- On Wayland the compositor owns final placement; the app requests
  the geometry, it cannot force it.

## Tray behaviour

- `QSystemTrayIcon` is created only if `isSystemTrayAvailable()`.
  Headless / tray-less sessions run fine without it.
- Icon is drawn in code (crimson disc + white `♪`), no asset file.
- Menu: Show Full App, Show Overlay, Play/Pause, Next, Previous,
  Display submenu, "Minimise to tray on close" (checkable, persisted),
  Quit.
- Double-click toggles the Full App (show if hidden, hide if shown).
- Tooltip updates on every position tick:
  `Artist — Title` + newline + current lyric line (truncated).
- This is a tray icon, not a KDE panel plasmoid — it does not add
  anything to the Plasma panel itself.

## Files and responsibilities

See [AGENTS.md](../AGENTS.md) for the file map. In short:

- `synced_lyrics.py` — UI + controller + CLI.
- `playback.py` — shared engine (the only poller / fetcher).
- `mpris.py` — player discovery, state, controls.
- `lyrics.py` — LRC parsing, LRCLIB client + cache, demo fixture.
- Config: `~/.config/synced-lyrics-linux/config.json`.
- Cache: `~/.cache/synced-lyrics-linux/` (lyrics JSON + `art/`).
