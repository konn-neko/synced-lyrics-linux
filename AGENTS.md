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
| `synced_lyrics.py` | Entry point + all UI: `FullAppWindow`, `OverlayWindow` (+ `ClickThroughHandle`), `AppController`, tray, display picker, `PreferencesDialog` (File > Preferences…, Ctrl+,), CLI (`--mode`, `--screen`, `--demo`, `--test-lyrics`). |
| `koneko_lyrics.py` | 3-line shim: imports `main` from `synced_lyrics.py`. Kept so the old `python3 koneko_lyrics.py` command still works. |
| `playback.py` | `PlaybackEngine` — the ONE shared state object. Single 250 ms MPRIS poller, `MonotonicPosition` interpolated position (never goes backwards — see MPRIS), lyrics fetch in a worker thread via `lyrics.fetch_lyrics` (provider order/enabled/word-only come from `engine.set_lyrics_prefs`). Both windows + tray render from it; never create a second poller. `word_index_at(pos)` gives the (line, word) active at a position. A new track clears the displayed lyrics AT ONCE in `_fetch_lyrics` (and manual search / re-fetch clear too) — the old song's lines must never linger while the new lookup runs. |
| `mpris.py` | MPRIS client. Primary: `PySide6.QtDBus`. Fallback: `playerctl` CLI. `list_players()`, `get_track()`, `play_pause()`, `next_track()`, `previous_track()`, `seek()`. `TrackInfo` dataclass. |
| `lyrics.py` | TTML + LRC + QRC + yrc parsers and seven lyrics clients. `PROVIDERS` registry (key, label, blurb) + `DEFAULT_PROVIDER_ORDER` + `normalise_provider_prefs()` — the order is user-editable in Preferences, so never hardcode the chain anywhere else. `fetch_lyrics()` is the ONE auto-fetch chain (order/enabled/word_only params). `parse_ttml()`, `parse_lrc()` (incl. Enhanced-LRC word tags), `parse_qrc()` (QRC + NetEase yrc), `fetch_betterlyrics()`, `fetch_unison()`, `fetch_amll()`, `fetch_qq()`, `fetch_netease()`, `fetch_lrclib()`, `fetch_kugou()`, `search_all_candidates()` (same prefs params), `LyricsResult`, `LyricLine`, `LyricWord`, `LRCLIBRateLimited`, `demo_result()` (demo lines get proportional word timings so `--demo` shows word highlight). |
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
  glide gets retargeted forever. Repeated or slightly behind reports
  do not rewind the interpolated anchor; a backward report jump over
  0.5 s or a forward jump over 2 s is treated as a seek. The anchor
  freezes while paused, and `seek()` sets it only on success. QtDBus
  listens to the selected player's `Seeked(qlonglong)` event (microseconds)
  and immediately applies external events, including seeks to old positions.
  A bounded list of local targets identifies delayed local acknowledgements,
  including overlapping seeks, but only while the target is younger than
  three seconds: `Seeked` has no request ID, so a longer window would swallow
  a genuine later seek to the same position. Track/player switches clear this
  state. Position-only reconciliation protects a local/event seek from
  isolated conflicting reports, surfaces uncertainty in the status bar, and
  adopts a consistent conflicting run after three seconds with no gap over
  1.5 seconds between reports. Protection is itself capped at ten
  seconds: if reports never converge the anchor follows the player and the
  status bar says the seek was not confirmed. A stale report is still
  indistinguishable from an unannounced external seek, so the fallback is
  evidence-based, never proof; status changes always apply. The fallback also
  covers players that never send `Seeked` even when the bus subscription
  succeeds.
  `_emit_position()` additionally clamps to the last emitted value
  (`_emit_floor`, reset on detected seek / track change / player change) so
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
  are wired in too (see "Word-sync sources" below): Kugou returns
  line-level LRC, QQ returns QRC XML — not TTML, but both are real
  providers now.
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
- Lyrics picker + re-pull: when several versions match a track, **Lyrics
  > Choose lyrics…** (also in the overlay right-click menu and the tray
  menu) searches every provider and lists every match — word-synced
  first, each with artist, album, duration, sync flags and provider —
  and the pick applies immediately. The pick is cached verbatim under
  the current track's key (`cache_lrclib_record`), so the next lookup
  loads it directly. **Lyrics > Re-fetch lyrics (ignore cache)** drops
  all seven cache files for the track (`clear_track_cache`: bl-, un-,
  am-, qq-, ne-, kg-, lrclib) and pulls fresh — for when the cached lyrics are wrong.
  Worker side: `_LyricsWorker.search` calls `search_all_candidates`
  and `PlaybackEngine.search_lyrics_candidates` / `apply_candidate` /
  `refetch_lyrics` (fetch takes `force`, which passes `use_cache=False`).
  Side fix in the same round: the LRCLIB cache write used to rebuild
  syncedLyrics from parsed lines, which dropped Enhanced-LRC word
  tags — a cached track lost word sync on reload. It now caches the
  raw record verbatim.
- Word-sync sources: seven providers, no keys, no logins. **Better
  Lyrics TTML** (`/getLyrics`, syllable-level) is first; **Unison**
  (`unison.betterlyrics.org/lyrics`, community uploads, TTML or LRC)
  second; **AMLL TTML Database** (`api.amll.dev`, hand-timed
  syllable TTML — search returns metadata only, lyrics come from a
  second GET by id; small corpus, best accuracy when it has the
  song) third; **QQ Music QRC** (`/qq/getLyrics`, word-level,
  `parse_qrc` — line `[startMs, durMs]`, word timing follows the
  word text, whitespace-only segments are separators) fourth;
  **NetEase** (`music.163.com/api/search/get` then
  `/api/song/lyric`, word-level `yrc` — same shape as QRC, parsed
  with `parse_qrc` — when it is empty the line `lrc` is used; some
  recordings have no lyrics at all, "暂无歌词") fifth; **LRCLIB**
  (line-level, some records Enhanced-LRC word tags) sixth; **Kugou**
  (`/kugou/getLyrics`, line-level LRC only) last. Auto-fetch tries
  them in that order, but word sync beats order (see Word-first
  below); the picker (`search_all_candidates`) merges
  all seven and sorts word-synced first, uncensored above censored
  at the same sync level, provider order within that. Caches:
  `bl-<key>.json` (TTML), `un-<key>.json` (Unison),
  `am-<key>.json` (AMLL TTML), `qq-<key>.json` (QRC),
  `ne-<key>.json` (yrc + lrc), `kg-<key>.json` (LRC),
  `<key>.json` (LRCLIB record). `clear_track_cache` removes all
  seven. Gotcha: QRC's LyricContent must be pulled from the raw
  XML with a regex — an XML parser normalizes its newlines to
  spaces and merges every line into one. Gotcha 2: AMLL TTML resets
  the default namespace inside `<div>` (`xmlns=""`), so its
  `<p>`/`<span>` elements are in NO namespace — `parse_ttml`
  matches elements by local name, not by the TTML namespace, or it
  finds zero lines. Gotcha 3: AMLL puts the space at the START of
  the next word's span (" beat") where Better Lyrics puts it at the
  END of the previous one ("Look ") — `parse_ttml` ends the word
  being built when a span's text starts with whitespace, or the
  sweep merges whole chunks. NetEase's `/api/search/get/web`
  variant returns an ENCRYPTED blob — the plain `/api/search/get`
  is the one to use. Musixmatch (needs an API key) and Apple Music
  (needs a developer token) are NOT viable under the no-keys rule,
  so they are not wired in.
- Word-first auto-fetch (`fetch_lyrics` in lyrics.py): the chain
  returns the first result WITH word sync even when an earlier
  provider already returned line-only lyrics — Unison's line-only
  LRC must not beat AMLL's word-synced TTML, and NetEase's line
  fallback must not beat LRCLIB's word-tagged record. It keeps
  looking past line-only (or plain-only) results while a later
  provider could still return word sync (`WORD_CAPABLE` — every
  provider except Kugou, which is line-only by nature); only when
  none does does the first line-synced result win.
- De-censoring (`decensor_result` in lyrics.py, on by default,
  Preferences > Sync > "De-censor lyrics using other sources",
  config `decensor`): when the auto-fetch winner is censored, the
  app asks `search_all_candidates` for the other providers' copies
  of the same track and swaps verified asterisked words from
  uncensored donors with synced lines. A word is swapped only when
  `_uncensored_word` proves it — same length, every non-`*`
  letter matches at the same position (`f**k` → `fuck`, `Sh*t` →
  `shit`); a donor's dropped-g spelling such as `fuckin'` can match
  `f**king` when the winner's `g` is visible. A fully-asterisked word
  (`****`) requires two independent providers to agree on the complete
  masked phrase, plus either at least two matching anchors on the same line or matching adjacent
  lines for a fully masked line. A donor word that is itself
  asterisked never matches. "The same line" is an
  alignment (`_align_lines`, difflib over the words' cores), NOT
  an exact match: different copies disagree on uncensored words
  (one drops a "like", "girly" vs "girlie", "shashasha" vs
  "chachacha"), so the donor line only needs its anchor words to
  line up in order at the same timestamp — a censored word with no
  donor counterpart (the donor drops it) is left alone rather than
  guessed. Copies also split lines differently (one line in the
  winner can be two lines in the donor), so runs of 2–3 consecutive
  donor lines are also tried
  merged, with the winner line falling inside the merged run's
  time span. And every uncensored donor is tried line by line —
  the first donor may fix some lines while a later one fixes the
  rest. Timings ALWAYS stay the winner's own; only a fixed word's
  syllable parts collapse to the whole word. The copy is flagged
  `LyricsResult.decensored` with `decensored_from` naming the
  donor that supplied the most fixed words (ties: first in pool
  order); `raw_text` keeps the original censored file (the debug
  view stays honest), and `is_censored` checks the fixed lines
  instead, so a fully fixed result reports uncensored and a
  partially fixed one still reports censored. The status bar names
  the donor (`… (de-censored from LRCLIB)`); the picker labels such
  a row "de-censored". The picker itself is never de-censored — it
  shows the raw candidates, uncensored first. Note: `is_censored_text`
  checks the token's core (via `_split_punct`), so a bare `****`
  with punctuation attached (`****,`) still counts as censored.
  Regression tests: `test_decensor_line_variants` (Y2K & bbno$ —
  Lalala, real NetEase vs LRCLIB lines) and
  `test_decensor_split_lines` (Ashnikko — Toxic, split lines +
  multi-donor).
- Preferences dialog layout: the content lives in a `QScrollArea`
  (`setWidgetResizable(True)`, horizontal scrollbar off) with the
  OK/Cancel bar outside it — on a small display the dialog scrolls
  instead of squashing (the first version's plain layout crushed
  the sources list to one row). Long labels (`sources_desc_label`,
  cache label) have `setWordWrap(True)`. The list keeps
  `setMinimumHeight(200)` + an expanding size policy. Tests:
  `test_preferences_dialog_layout` shrinks the dialog to 560x480
  and asserts the list stays ≥200px tall.
- Explicit / censored lyrics: there is NO explicit filter on any
  provider — the censoring is baked into the data. Better Lyrics
  TTML is uncensored (Kendrick — HUMBLE. returns fuck/shit/bitch
  intact); QQ and Kugou are always asterisked (`f**k`, `ni**a`,
  `p**sy` — same censored upstream); LRCLIB varies by record. The
  picker labels it instead: `LyricsResult.is_censored` (via
  `is_censored_text(raw_text)` — a token with `*` next to letters,
  or a bare `****`; a lone `*` does not count) is shown as a
  "censored" tag in `_candidate_label`, and `search_all_candidates`
  sorts uncensored above censored at the same sync level
  (rank = sync, censored, provider). Auto-fetch already prefers
  TTML, so the explicit version wins automatically when it exists.
  Workaround when TTML has no cached copy: play the track once in
  the Better Lyrics browser extension — it caches the uncensored
  TTML, and the app picks it up from then on.
- Debug: `lyrics.py` logs EVERY HTTP request it makes (Better Lyrics
  + Unison + AMLL + NetEase + LRCLIB + QQ + Kugou, via `log_request(url, status, body)` in
  `_get_json`, `fetch_betterlyrics`, `_fetch_betterlyrics_provider`
  and `_get_json_url`; album-art fetches in `synced_lyrics.py` `_load_art` log too) —
  last 20, bodies capped at 200k chars.
  `LyricsResult` carries `raw_text` / `raw_kind` (the raw TTML or LRC
  exactly as received) and `cache_file`. Help > Lyrics debug
  (`LyricsDebugDialog`, also in the overlay right-click menu, via
  `AppController.show_lyrics_debug`) shows `build_lyrics_debug_text`
  (track, source, raw file, request log) in a monospace read-only
  view with Copy all. Never commit cached lyrics or raw TTML.
- Preferences: **File > Preferences…** (Ctrl+,; also in the overlay
  right-click menu and the tray menu) opens `PreferencesDialog`
  (synced_lyrics.py). It works on a copy of the cfg — Cancel discards
  everything, OK hands `new_cfg` to `AppController.apply_preferences`,
  which saves and refreshes every window + the engine. Sections:
  **Lyrics sources** — the seven providers as a checkable list with
  Move up / Move down / Restore defaults; order is the auto-fetch
  order (top first), unchecked providers are skipped by auto-fetch
  AND the picker. **Sync** — `word_only` checkbox ("Only use
  word-synced lyrics": line-only results are skipped in the chain and
  hidden from the picker; a track with only line sync shows "No
  word-synced lyrics found") + sync offset spinbox. **Appearance** —
  font size, line animations toggle, "Open at launch" (Full App /
  Overlay, config `start_mode`; `--mode` still wins). **Overlay** —
  opacity slider, click-through. **Behaviour** — minimise to tray,
  display picker. **Lyrics cache** — track count + size readout and
  a Clear button (deletes `*.json` in the cache dir, keeps art).
  Applying prefs re-fetches the current track via
  `engine.set_lyrics_prefs` (the displayed lyrics may have come from
  a provider that is now off, or be line-only under a new word-only
  rule). Config keys: `provider_order`, `provider_enabled`,
  `word_only`, `start_mode`. `normalise_provider_prefs` (lyrics.py)
  cleans a stale config: unknown keys dropped, missing providers
  appended at the end (on) — so a new provider added by an update is
  never silently absent. The no-match status names the enabled count:
  "No lyrics found (N providers checked)".
- New track clears lyrics immediately: `_fetch_lyrics` sets
  `self.lyrics = None` + `lyrics_changed.emit(None)` before the
  worker fetch starts, so the previous song's lines never linger
  while the new lookup runs (seven providers, network round-trips).
  Same on manual search and on re-fetch (cached lyrics there are
  known wrong). The stale-result guard (`key != _fetch_key`) still
  drops late results for a track already left.
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
  overlay_click_through, line_animations, last_player,
  provider_order, provider_enabled, word_only, decensor, start_mode).
- Lyrics cache: `~/.cache/synced-lyrics-linux/*.json` — LRCLIB records
  as `<sha256>.json`, Better Lyrics TTML as `bl-<sha256>.json`,
  Unison as `un-<sha256>.json`, AMLL as `am-<sha256>.json`, QQ QRC as
  `qq-<sha256>.json`, NetEase as `ne-<sha256>.json`, Kugou as
  `kg-<sha256>.json`.
- Album art cache: `~/.cache/synced-lyrics-linux/art/`.

## How to run tests headless

```bash
python3 -m py_compile synced_lyrics.py koneko_lyrics.py lyrics.py mpris.py playback.py test_lyrics.py test_ui_smoke.py
python3 test_lyrics.py            # LRC/TTML parse + live provider checks (test_live_* SKIP when offline or uncached, never fail for no network)
LYRICS_LIVE_TESTS=0 python3 test_lyrics.py   # skip every test_live_* check (what CI runs: no provider outage can fail the build)
QT_QPA_PLATFORM=offscreen ~/.local/lyrics-venv/bin/python test_ui_smoke.py   # animation helpers, render_line_html, MonotonicPosition, single-instance lock + local server, demo word highlight
  (Test venv lives at `~/.local/lyrics-venv` — NOT /tmp: /tmp is a
  512M tmpfs and a fresh PySide6 install does not fit, and /tmp is
  wiped on VM replacement. The venv also needs system libEGL —
  installed 2026-11-14 from the libegl1 + libegl-mesa0 .debs after
  `apt-get update` hung on a mirror.)
  No-root recipe (used when the image has no sudo and empty apt lists):
  `apt-get -o Dir::State::lists=/tmp/aptdl/lists -o Debug::NoLocking=1 update`,
  then `apt-get -o Dir::State::lists=/tmp/aptdl/lists -o Debug::NoLocking=1
  download libxkbcommon0 libxkbcommon-x11-0 libegl1 libglvnd0 libfreetype6
  libfontconfig1 libexpat1 libuuid1 libpng16-16t64 libbrotli1 libbz2-1.0
  libdbus-1-3`, `dpkg-deb -x` each .deb into `~/.local/qtlibs`, and run with
  `LD_LIBRARY_PATH=~/.local/qtlibs/usr/lib/x86_64-linux-gnu`. With that, the
  FULL `test_ui_smoke.py` suite runs offscreen, passes every check and
  exits 0. (Fontconfig may log one harmless "Cannot load default config
  file" warning; unpacking `fonts-dejavu-core` plus a `fonts.conf` silences
  it but is not needed for a clean exit.)
  `main()` runs the tests via `_run_suite(app)` and tears down in a
  `finally` with `_teardown_qt()`: that closes and deletes the tests'
  top-level windows, pumps the event loop, then destroys the QApplication
  itself (`deleteLater` + DeferredDelete, so `QCoreApplication.instance()`
  is None afterwards). Qt objects must not outlive the application or
  PySide6 crashes during interpreter shutdown — a random SIGSEGV, and a
  reproducible SIGBUS after the `PreferencesDialog` tests. `main()` still
  holds its own `app` reference, so deleting the name inside the helper
  would release nothing. A failing test exits 1, not 139, so the exit code
  is CI-trustworthy: `.github/workflows/tests.yml` runs `test_lyrics.py`
  with `LYRICS_LIVE_TESTS=0` plus this suite offscreen under
  `dbus-run-session` on PRs and pushes to main. The smoke step does not
  trust exit 0 alone: it fails on any `SKIP` line (so a new check needs no
  CI edit, but a skipped one can never pass), requires the
  `MPRIS Seeked subscription: OK` line, and requires the suite's final
  `test_ui_smoke: OK` line, so a shrunken run fails the job. A
  `concurrency` group cancels the run a newer push replaces.
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
2. Register it in `lyrics.py: PROVIDERS` (key, label, blurb) and in
   `_fetcher_map()` — the chain lives in `fetch_lyrics()` and the
   picker in `search_all_candidates()`; both read the registry order,
   and the user can reorder/disable the provider in Preferences, so
   never hardcode the chain in `playback.py` (the worker only passes
   the engine's prefs through). Word-sync sources go before line-only
   ones in `DEFAULT_PROVIDER_ORDER`. A provider that returns None on
   401/429 must never fail the track.
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
