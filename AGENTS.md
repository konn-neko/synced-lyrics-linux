# AGENTS.md — synced-lyrics-linux

For AI agents (and humans) working on this repo.
Repo: https://github.com/konn-neko/synced-lyrics-linux

## What this is

Native Linux synced-lyrics app, a BetterLyrics alternative for Linux.
Reads what's playing via MPRIS, fetches time-synced lyrics from LRCLIB,
shows them in a Full App window and/or a floating Overlay, with a system
tray icon. Python 3 + PySide6 (Qt6), stdlib only for HTTP. No Electron,
no login, no API keys.

## Architecture map

| File | Does what |
|---|---|
| `synced_lyrics.py` | Entry point + all UI: `FullAppWindow`, `OverlayWindow` (+ `ClickThroughHandle`), `AppController`, tray, display picker, CLI (`--mode`, `--screen`, `--demo`, `--test-lyrics`). |
| `koneko_lyrics.py` | 3-line shim: imports `main` from `synced_lyrics.py`. Kept so the old `python3 koneko_lyrics.py` command still works. |
| `playback.py` | `PlaybackEngine` — the ONE shared state object. Single 250 ms MPRIS poller, position interpolation, lyrics fetch in a worker thread. Both windows + tray render from it; never create a second poller. |
| `mpris.py` | MPRIS client. Primary: `PySide6.QtDBus`. Fallback: `playerctl` CLI. `list_players()`, `get_track()`, `play_pause()`, `next_track()`, `previous_track()`, `seek()`. `TrackInfo` dataclass. |
| `lyrics.py` | LRC parser + LRCLIB client + demo fixture. `parse_lrc()`, `fetch_lrclib()`, `LyricsResult`, `LyricLine`, `LRCLIBRateLimited`, `demo_result()`. |
| `test_lyrics.py` | Tests for `lyrics.py` only (no Qt needed). Runnable as `python3 test_lyrics.py` or `pytest`. |
| `install.sh` | CachyOS/Arch one-command install: pacman `pyside6` + `playerctl`, launcher `~/.local/bin/synced-lyrics`, desktop file `~/.local/share/applications/synced-lyrics-linux.desktop`. |
| `requirements.txt` | `PySide6>=6.6` — pip fallback only; prefer the distro package. |
| `docs/ARCHITECTURE.md` | Data-flow diagram + mode/tray/display behaviour. |

## MPRIS

- Discover players via `org.freedesktop.DBus` `ListNames`, prefix
  `org.mpris.MediaPlayer2.` — never hardcode player names (Spotify,
  firefox, chromium, vlc, … all appear the same way).
- Per player: `org.mpris.MediaPlayer2.Player` properties `Metadata`
  (`xesam:title`, `xesam:artist` list, `xesam:album`, `mpris:length` in
  µs, `mpris:artUrl`), `Position` (µs), `PlaybackStatus`.
- Controls: `PlayPause`, `Next`, `Previous` methods; `seek()` uses
  `playerctl position <seconds>` first (it handles the track id), with
  an MPRIS `SetPosition` fallback.
- Browsers report position in 1 s steps. `PlaybackEngine` interpolates
  with `time.monotonic()` between 250 ms polls — do not poll faster
  than 250 ms to "fix" browser granularity; interpolation is the fix.
- If QtDBus fails / no players, fall back to `playerctl -l` and
  `playerctl --player <name> metadata --format …`. If neither works,
  the engine reports "No MPRIS player found" and keeps polling.

## LRCLIB

- Base: `https://lrclib.net`. No key, no account.
- `GET /api/get?artist_name=&track_name=&album_name=&duration=`
  (duration matched within ±2 s by the server). 404 → fall back to
  `GET /api/search?q=<artist> <track>`, prefer synced over plain-only,
  then closest duration.
- Headers: `Lrclib-Client: synced-lyrics-linux/1.0`, `Accept: application/json`.
- Response fields used: `syncedLyrics` (LRC), `plainLyrics`,
  `instrumental` (bool), `duration`, `id`, `artistName`, `trackName`,
  `albumName`.
- Rate limits: ~45 req/min generic, ~90/min identified. A 429 raises
  `LRCLIBRateLimited(retry_after)` — honour `Retry-After`, show it in
  the status bar, do not retry immediately.
- Cache: `~/.cache/synced-lyrics-linux/<sha256>.json`, one JSON file per
  track (raw LRCLIB record + `fetched_at`). Cache reads return
  `source="cache"`. Cache dir is gitignored — see "What NOT to do".
- Only artist/title/album/duration are sent to LRCLIB. Nothing else.

## LRC parsing rules (`lyrics.py: parse_lrc`)

- `[mm:ss.xx]` and `[mm:ss]`; `.x` = tenths, `.xx` = hundredths,
  `.xxx` = milliseconds.
- Multiple timestamps per line → one `LyricLine` per timestamp, same text.
- `[offset:+/-ms]` shifts all lines; clamp negative results to 0.
- Metadata tags (`[ti:]`, `[ar:]`, …) and blank lines are ignored.
- Enhanced word tags `<mm:ss.xx>` are stripped; the line text is kept
  and the line timestamp is used (line sync, not word sync).
- Output is sorted by time. `LyricsResult.line_at(pos, offset)` returns
  the last line with `time <= pos + offset`, or -1.

## UI modes and shared state

- `AppController` owns the single `PlaybackEngine`. `FullAppWindow`
  and `OverlayWindow` only connect to its signals (`track_changed`,
  `lyrics_changed`, `position_changed`, `players_changed`,
  `status_changed`) and call its control methods. Do not add a second
  timer/poller in a window.
- Mode switching: `Ctrl+1` Full App, `Ctrl+2` Overlay; Full App button
  "Pop out overlay"; Overlay double-click / menu / `Ctrl+1` returns.
  `--mode full|overlay` picks the start mode.
- Overlay: frameless, `WindowStaysOnTopHint`, `Qt.Tool` (no taskbar
  entry), translucent dark background, `QSizeGrip`, drag by mouse,
  opacity via `setWindowOpacity`, click-through via
  `WA_TransparentForMouseEvents` (Qt6; Qt5: `WA_TransparentForInput`) + a separate `ClickThroughHandle` widget
  (the overlay itself ignores clicks in that mode, so the handle is
  the only way back — keep it).
- Tray: `QSystemTrayIcon`, icon drawn in code (`make_tray_icon`, a `♪`
  on a crimson disc). Guard with `isSystemTrayAvailable()` — headless /
  tray-less sessions must still run. Double-click toggles Full App.
  Tooltip: `Artist — Title\n<current line>`.
- Display: `QGuiApplication.screens()`, labels
  `Display N — WxH (primary)`. Choice stored in
  `~/.config/synced-lyrics-linux/config.json` as `"display"`:
  `"primary"` | `"follow"` | int index. Missing display → primary.
  Overlay follows the choice immediately; Full App moves only via the
  explicit menu action. `--screen N` sets the choice for that launch.

## Config / cache locations

- Config: `~/.config/synced-lyrics-linux/config.json`
  (offset, font_size, display, minimise_to_tray, overlay_opacity,
  overlay_click_through, last_player).
- Lyrics cache: `~/.cache/synced-lyrics-linux/*.json`.
- Album art cache: `~/.cache/synced-lyrics-linux/art/`.

## How to run tests headless

```bash
python3 -m py_compile synced_lyrics.py koneko_lyrics.py lyrics.py mpris.py playback.py test_lyrics.py
python3 test_lyrics.py            # LRC + live LRCLIB (SKIP if offline, never fails for no network)
QT_QPA_PLATFORM=offscreen timeout 8 python3 synced_lyrics.py --demo
QT_QPA_PLATFORM=offscreen timeout 8 python3 synced_lyrics.py --mode overlay --demo
QT_QPA_PLATFORM=offscreen timeout 8 python3 synced_lyrics.py --mode overlay --screen 1 --demo
python3 synced_lyrics.py --test-lyrics "Coldplay" "Yellow"
```

Headless runs need a timeout — the app runs until quit by design.
`--demo` needs no player, no network, no tray. The overlay test
verifies the overlay window constructs and the `--screen` flag parses;
offscreen has one virtual display, so `--screen 1` falls back to primary.

## How to add a lyrics provider

1. Add a `fetch_<provider>()` in `lyrics.py` returning `LyricsResult`
   (or `None`), same shape as `fetch_lrclib()`.
2. Call it from `playback.py: _LyricsWorker.fetch` as a fallback chain
   (LRCLIB first, then the new provider) — keep LRCLIB first.
3. Set `result.source` to the provider name so the status bar is honest.
4. Add a parser test in `test_lyrics.py` if the format differs from LRC.
5. Document the provider, its rate limits, and whether it needs a key
   in README.md. Prefer keyless providers.

## What NOT to do

- No API keys, no logins, no accounts, no OAuth. Ever.
- No committing cached lyrics: `~/.cache/synced-lyrics-linux/` is
  gitignored, and no lyric text (beyond the short demo excerpt in
  `lyrics.py`) belongs in the repo. Lyrics are fetched at runtime.
- No hardcoding player names, screen sizes, or William's local paths.
  Players come from MPRIS discovery; displays from `screens()`.
- No second MPRIS poller. One engine, shared by all windows.
- No Electron, no webview, no extra runtime dependencies beyond PySide6.
- No pushing to GitHub from an agent run — repo creation/push is done
  by the maintainer (parent agent). Make the folder push-ready instead.
- Do not "fix" browser 1 s position granularity by polling faster;
  interpolation is the intended mechanism.
