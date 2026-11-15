# AGENTS.md — synced-lyrics-linux

For AI agents (and humans) working on this repo.
Repo: https://github.com/konn-neko/synced-lyrics-linux

## What this is

Native Linux synced-lyrics app, a BetterLyrics alternative for Linux.
Reads what's playing via MPRIS, fetches word-synced lyrics from the
Better Lyrics API (TTML) with LRCLIB line sync as fallback, shows them
in a Full App window and/or a floating Overlay, with a system
tray icon. Python 3 + PySide6 (Qt6), stdlib only for HTTP. No Electron,
no login, no API keys.

## Architecture map

| File | Does what |
|---|---|
| `synced_lyrics.py` | Entry point + all UI: `FullAppWindow`, `OverlayWindow` (+ `ClickThroughHandle`), `AppController`, tray, display picker, CLI (`--mode`, `--screen`, `--demo`, `--test-lyrics`). |
| `koneko_lyrics.py` | 3-line shim: imports `main` from `synced_lyrics.py`. Kept so the old `python3 koneko_lyrics.py` command still works. |
| `playback.py` | `PlaybackEngine` — the ONE shared state object. Single 250 ms MPRIS poller, `MonotonicPosition` interpolated position (never goes backwards — see MPRIS), lyrics fetch in a worker thread (Better Lyrics TTML first, LRCLIB fallback). Both windows + tray render from it; never create a second poller. `word_index_at(pos)` gives the (line, word) active at a position. |
| `mpris.py` | MPRIS client. Primary: `PySide6.QtDBus`. Fallback: `playerctl` CLI. `list_players()`, `get_track()`, `play_pause()`, `next_track()`, `previous_track()`, `seek()`. `TrackInfo` dataclass. |
| `lyrics.py` | TTML + LRC parsers and both lyrics clients. `parse_ttml()`, `parse_lrc()` (incl. Enhanced-LRC word tags), `fetch_betterlyrics()`, `fetch_lrclib()`, `LyricsResult`, `LyricLine`, `LyricWord`, `LRCLIBRateLimited`, `demo_result()` (demo lines get proportional word timings so `--demo` shows word highlight). |
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
- Monotonic position (`MonotonicPosition` in `playback.py`): browsers
  report Position in 1 s steps, so a naive anchor reset sawtooths the
  estimate back to the floored value on every poll — if a line boundary
  falls in that window, `line_at()` flips back and forth and the scroll
  glide gets retargeted forever. The anchor is adopted only when the
  report is ahead of the estimate minus 0.5 s, or more than 2.0 s away
  either way (a real seek); a report behind the estimate by more than
  0.5 s is stale and ignored. `seek()` sets the anchor explicitly.
  `_emit_position()` additionally clamps to the last emitted value
  (`_emit_floor`, reset on seek / track change / player change) so
  microsecond scheduling jitter can never flip the line index back.
  Demo mode keeps its own position logic and is untouched by this.
- If QtDBus fails / no players, fall back to `playerctl -l` and
  `playerctl --player <name> metadata --format …`. If neither works,
  the engine reports "No MPRIS player found" and keeps polling.

## Better Lyrics API (first provider)

- Base: `https://api.betterlyrics.org`. Docs: https://lyrics-api-docs.boidu.dev
- `GET /getLyrics?s=<title>&a=<artist>&al=<album>&d=<duration seconds>`
  (all four params — the cache key includes album+duration, so missing
  them usually misses the cache). 422 only if song+artist both missing.
- Response: `{"ttml": "<xml...>", "score": 0.97}` (score only on fresh
  fetches). TTML: `<p begin end>` per line, `<span begin end>` per
  word/syllable. Times: `HH:MM:SS.mmm`, `M:SS.mmm`, or bare seconds
  (`33.642`).
- Cache-first, NO API KEY in this codebase, ever. Cached songs are
  free; a cache miss without a key returns 401 and `fetch_betterlyrics`
  returns None so the caller falls back to LRCLIB — the track never
  fails for this. Keys are not being issued right now. 429 → return
  None (fall back) rather than blocking; honour `Retry-After` by not
  retrying. `/revalidate` needs a key and is not used.
- The Kugou (`/kugou/getLyrics`) and QQ (`/qq/getLyrics`) endpoints
  were checked live and are NOT wired in: they return
  `{"lyrics": ..., "provider": ...}` — line-level LRC for Kugou, QRC
  XML for QQ — not TTML. LRCLIB already covers line-level.
- Cache: `~/.cache/synced-lyrics-linux/bl-<sha256>.json` (raw TTML +
  `fetched_at`); cache reads re-parse identically, source
  `"betterlyrics-cache"` (network: `"betterlyrics"`).
- Only artist/title/album/duration are sent. Timeout 10 s.

## LRCLIB (fallback provider)

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

## LRC / TTML parsing rules (`lyrics.py`)

- LRC: `[mm:ss.xx]` and `[mm:ss]`; `.x` = tenths, `.xx` = hundredths,
  `.xxx` = milliseconds.
- Multiple timestamps per line → one `LyricLine` per timestamp, same text
  (word times shift by the tag difference for repeated lines).
- `[offset:+/-ms]` shifts all lines; clamp negative results to 0.
- Metadata tags (`[ti:]`, `[ar:]`, …) and blank lines are ignored.
- Enhanced word tags `<mm:ss.xx>` are PARSED into `LyricLine.words`
  (`LyricWord(time, end, text, is_background)`), not stripped: each
  word's end is the next word's start, the last word's end is the next
  line's start (last line: +3 s). A line with no word tags keeps
  `words=[]` (line-only sync).
- TTML (`parse_ttml`): `<p begin end>` → line, timed `<span>` →
  word/syllable. Spans without `begin` are wrappers — recurse into
  them, propagating `ttm:role="x-bg"` (background vocals). Whitespace
  between spans IS the space between words: a span whose text ends in
  whitespace, or whose tail holds whitespace, ends a word; spans with
  neither continue the same word (syllables). Never pretty-print the
  TTML — that destroys the inter-span whitespace.
- `LyricsResult.has_word_sync` is true when any line has words;
  `word_at(line_idx, pos, offset)` returns the active word index or -1.
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
- QMenu's parent must be a QWidget or None — NEVER a QApplication.
  The tray menu once passed the app as parent to `build_display_menu()`
  and crashed on real desktops at startup (`TypeError` in `QMenu.__init__`);
  headless tests missed it because the tray is unavailable offscreen.
  `build_display_menu()` now guards this, and `test_ui_smoke.py`
  (run offscreen) covers QApplication / QWidget / None parents.
- Line-change animation (Qt-native, no new deps; see `SmoothScroller`,
  `LabelFader`, `_lerp_color` in `synced_lyrics.py`):
  Full App glides to the new line — `QPropertyAnimation` on the
  vertical scrollbar's `value`, 350ms, `QEasingCurve.OutCubic` — while
  the highlight colour crossfades over ~150ms (5 x 30ms `QTimer`
  steps, `_lerp_color`, font/bold swap stays instant so row heights
  are final before the scroll target is computed). Overlay crossfades
  each label — `QGraphicsOpacityEffect` + one reused
  `QPropertyAnimation` on `opacity`, 150ms out, swap text, 250ms in,
  OutCubic; no geometry animation (labels stay layout-managed).
  One animation object per window, reused. Rapid line changes:
  the scroller stops and restarts from the current scroll value;
  the fader updates its pending text mid-fade-out or restarts the
  fade mid-fade-in; the Full App fade is finalised (`_finish_fade`)
  before the next highlight. `_on_lyrics` stops all animations
  BEFORE clearing the list (the fade holds items `clear()` deletes).
  Toggle: config `"line_animations"` (default `true`), Full App
  `View > Smooth line animations` + overlay right-click menu, via
  `AppController.set_line_animations()`; both windows read the cfg
  live at each line change. When off: instant `setValue` /
  instant `setText` + final colours, exactly the old behaviour.
  Tests: `test_ui_smoke.py` covers the helpers directly (glide,
  mid-glide retarget, mid-fade retarget, instant-when-off) — the
  windows themselves are only exercised by the `--demo` smoke run.
- Overlay: frameless, `WindowStaysOnTopHint`, `Qt.Tool` (no taskbar
  entry), translucent dark background, `QSizeGrip`, drag by mouse,
  opacity via `setWindowOpacity`, click-through via
  `WA_TransparentForMouseEvents` (Qt6; Qt5: `WA_TransparentForInput`) + a separate `ClickThroughHandle` widget
  (the overlay itself ignores clicks in that mode, so the handle is
  the only way back — keep it).
- Word-by-word highlight: pure helper `render_line_html(line, word_idx,
  sung_color, unsung_color)` in `synced_lyrics.py` (sung words crimson
  + bold, unsung dim / light grey, background words italic; no word
  timings → plain escaped text, exactly line-only mode). **QLabel
  renders this HTML natively, QListWidgetItem does NOT** — its default
  delegate draws item text as plain text, so the Full App showed the
  tags literally (`<span style=...>` on screen, William's screenshot).
  The list therefore uses `LyricsItemDelegate` (QStyledItemDelegate +
  QTextDocument paint/sizeHint, plain lines take the item foreground
  as base colour so the highlight crossfade still works) with
  `setWordWrap(True)` and no horizontal scrollbar. **Scroll mode:**
  the list MUST be `setVerticalScrollMode(ScrollPerPixel)` — the
  default ScrollPerItem measures the scrollbar in whole items, so the
  pixel target SmoothScroller computes was clamped to the item count
  and the list lurched between extremes (the up/down scroll bug that
  survived the monotonic-position fix). Full App:
  `_highlight()` applies it on line change and restores the plain text
  on the previous line; `_on_position()` word-only updates call
  `_render_current_words()` and must NOT call `_highlight()` (that
  would restart the scroll glide). Overlay: line changes crossfade to
  the word HTML via `LabelFader`; word-only updates set the label text
  directly, but only when the fader is idle (`LabelFader.busy`) — a
  skipped tick retries on the next 250 ms position tick. Engine side:
  `PlaybackEngine.word_index_at(pos)` → (line_idx, word_idx) from the
  same interpolated position; do not poll faster for words.
- Mid-word sweep (not word snap): TTML syllable spans are kept as
  `LyricWord.parts` on each word (`parse_ttml` merges spans into a
  word but keeps the spans with their own begin/end; ELRC/demo words
  get parts=[the word itself]). `render_line_html(line, word_idx, pos,
  offset)` renders the active word as a sweep: completed parts fully
  sung, later parts unsung. The part containing `pos` transitions
  smoothly: each character's colour interpolates between unsung and
  sung (`_lerp_hex`) as the sweep front passes through it — a
  gradient wash across the word, not whole characters snapping.
  `pos=None` renders the whole active word sung (the old behaviour).
  A 60 ms QTimer in each window (`_on_fill_tick`) re-renders the
  current line's fill from `engine.current_position()` and only calls
  setText when the HTML actually changed; it never touches MPRIS — it
  reads the monotonic estimate between 250 ms polls, and line changes
  still go through `_highlight()` / the overlay crossfade.
- Smooth sweep + glow (the pixel-smooth version of the above): HTML
  can only flip whole characters, so on a long held syllable the
  front visibly steps. The current line is therefore painted by
  `_paint_sweep_text` (shared by the Full App delegate and the
  overlay's `GlowWordLabel`): the front is a continuous pixel
  position from `_sweep_front` (float char position in the line),
  the sung text is drawn in crimson through a soft-edged alpha mask
  (~8 px fade past the front) with low-opacity offset copies as a
  glow, re-painted ~16x/s from the interpolated position. The
  delegate gets its data from `FullAppWindow._sweep_for_row`, the
  label from `OverlayWindow._sweep_for_label` (only while the line
  crossfade is idle). Both windows' `_on_fill_tick` also force a
  repaint every tick (viewport().update() / label.update()) so the
  front moves even while the HTML is unchanged. CAUTION: a
  `body { color: ... }` default stylesheet does NOT colour
  QTextDocument text in Qt (it renders black) — colours are set via
  QTextCursor char formats instead. The HTML path is still the
  fallback for every other row, and the item text is still set to
  the word HTML (tests + fallback).
- Debug: `lyrics.py` logs EVERY HTTP request it makes (Better Lyrics
  + LRCLIB, via `log_request(url, status, body)` in `_get_json` and
  `fetch_betterlyrics`; album-art fetches in `synced_lyrics.py`
  `_load_art` log too) — last 20, bodies capped at 200k chars.
  `LyricsResult` carries `raw_text` / `raw_kind` (the raw TTML or LRC
  exactly as received) and `cache_file`. Help > Lyrics debug
  (`LyricsDebugDialog`, also in the overlay right-click menu, via
  `AppController.show_lyrics_debug`) shows `build_lyrics_debug_text`
  (track, source, raw file, request log) in a monospace read-only
  view with Copy all. Never commit cached lyrics or raw TTML.
- Single instance: `main()` takes a `QLockFile` at
  `~/.config/synced-lyrics-linux/synced-lyrics.lock` (held on the app
  object for the run; `tryLock(0)` — PySide6 requires the timeout arg;
  stale locks from crashed runs are reclaimed by Qt). A second launch
  connects via `QLocalSocket` to the first instance's `QLocalServer`
  (`"synced-lyrics-linux"`), which raises its Full App, and the second
  process exits 0. `--test-lyrics` returns before the lock (CLI mode
  must not take it). Closing the window still minimises to tray — the
  guard, not a behaviour change, stops the pile-up of tray icons.
- Clean quit: `AppController.quit_app()` hides the tray icon, overlay,
  click-through handle and Full App window, then `engine.stop()` and
  `app.quit()`; `aboutToQuit` also hides everything, so no ghost ♪
  stays in the KDE panel after quit from any path.
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
  overlay_click_through, line_animations, last_player).
- Lyrics cache: `~/.cache/synced-lyrics-linux/*.json` — LRCLIB records
  as `<sha256>.json`, Better Lyrics TTML as `bl-<sha256>.json`.
- Album art cache: `~/.cache/synced-lyrics-linux/art/`.

## How to run tests headless

```bash
python3 -m py_compile synced_lyrics.py koneko_lyrics.py lyrics.py mpris.py playback.py test_lyrics.py test_ui_smoke.py
python3 test_lyrics.py            # LRC/TTML parse + live Better Lyrics + live LRCLIB (live tests SKIP when offline or uncached, never fail for no network)
QT_QPA_PLATFORM=offscreen /tmp/lyrics-venv/bin/python test_ui_smoke.py   # animation helpers, render_line_html, MonotonicPosition, single-instance lock + local server, demo word highlight
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
   (or `None`), same shape as `fetch_betterlyrics()` / `fetch_lrclib()`.
2. Call it from `playback.py: _LyricsWorker.fetch` in the chain
   (Better Lyrics TTML first, LRCLIB second — keep that order unless
   the new provider has word sync and a keyless cache-first model).
   A provider that returns None on 401/429 must never fail the track.
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
