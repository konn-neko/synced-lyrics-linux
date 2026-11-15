# synced-lyrics-linux

**Native Linux synced lyrics — a BetterLyrics-style app for Linux.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Qt6 / PySide6](https://img.shields.io/badge/Qt-6%20%2F%20PySide6-green.svg)](https://doc.qt.io/qtforpython/)

> **Repo:** https://github.com/konn-neko/synced-lyrics-linux

BetterLyrics is Windows-only (WinUI 3, Windows Media Transport Controls).
On Linux the same job is actually easier: every player already publishes
what's playing over **MPRIS**, and **Better Lyrics** serves word-synced
lyrics (TTML, cache-first, no key) — plus its QQ provider (QRC,
word-level) — with **LRCLIB** and Kugou as line-sync fallbacks. This
app glues those together in a native Qt6 window — no Electron, no
account, no API key.

## Screenshots

<!-- Placeholder — replace with real screenshots after the first run on CachyOS. -->
<!-- ![Full App](docs/screenshots/full-app.png) -->
<!-- ![Overlay](docs/screenshots/overlay.png) -->

_Screenshots wanted: Full App window mid-song, and the Overlay floating
over a desktop. See [CONTRIBUTING.md](CONTRIBUTING.md)._

## Features

- **Full App window** — Now Playing bar with album art, progress slider,
  Play/Pause / Prev / Next, player selector, sync-offset and font-size
  controls, manual search, and a scrollable lyrics pane. The current line
  is highlighted in crimson (`#FF1A47`), upcoming lines in obsidian blue
  (`#466EA5`), past lines dimmed. Click a line to seek to it.
  Line changes are animated, not instant: the list glides to the new
  line (~350ms smooth scroll) while the highlight colour crossfades
  (~150ms). Turn it off in `View > Smooth line animations` (or
  `"line_animations": false` in the config) for the old instant swap.
- **Word-by-word karaoke** — when a track has word timings, words
  light up one by one in crimson as they're sung (Full App list and
  Overlay alike), instead of the whole line flipping at once. Better
  Lyrics TTML carries syllable timings too, so the colour washes
  across a word mid-word, syllable by syllable, instead of the whole
  word snapping at once — the character the sweep is passing through
  fades smoothly from dim to crimson as it's sung. The current line
  is painted pixel-smooth with a soft edge and a crimson glow around
  the sung part, so on a long held note the highlight glides
  continuously instead of stepping letter by letter. Tracks without word timings fall back to
  line-only highlighting, exactly as before. `--demo` shows the
  effect on the sample lyrics.
- **Choose lyrics** — if several versions of a song exist, `Lyrics >
  Choose lyrics…` (also in the Overlay menu and the tray menu) lists
  every match from all four providers — Better Lyrics (syllable-sync),
  QQ Music (word-sync), LRCLIB and Kugou — word-synced first, each
  labelled with its provider, and rows whose lyrics are
  asterisk-censored (`f**k`, `ni**a` …) carry a "censored" tag and
  sort below uncensored matches at the same sync level. The pick
  applies immediately and sticks for that track. `Lyrics > Re-fetch
  lyrics (ignore cache)` drops the cached lyrics and pulls fresh,
  for when the cache has the wrong version.
- **Lyrics debug** — `Help > Lyrics debug` (also in the Overlay
  right-click menu) shows the raw lyrics file exactly as received
  plus every URL query the app made this run with its response, so
  you can see what the APIs actually returned.
- **Overlay mode** — frameless, always-on-top, translucent. Shows the
  current line large + the next line dimmed, with track title/artist and
  play/pause controls. Lines crossfade on change (~150ms out, ~250ms in)
  instead of snapping. Draggable, resizable, double-click to return to
  the Full App. Right-click menu has opacity slider and click-through
  toggle (clicks pass through, except a small `◉` handle that turns
  click-through back off).
- **Swap freely** — Full App has a "Pop out overlay" button; View menu
  and shortcuts switch modes: `Ctrl+1` Full App, `Ctrl+2` Overlay.
  Both windows share one `PlaybackEngine` — one MPRIS poller, one lyrics
  fetcher — so switching modes never loses sync or restarts playback.
- **System tray** — a `♪` tray icon (drawn in code, no asset needed)
  while the app runs. Menu: Show Full App / Show Overlay, Play/Pause,
  Next, Previous, Display picker, Quit. Double-click toggles the Full
  App. Tooltip shows `Artist — Title` + the current lyric line.
  Closing the Full App minimises to tray instead of quitting (toggle in
  File menu / tray menu; Quit in the tray or File menu actually exits).
  This is a tray icon, not a KDE panel plasmoid.
- **Per-display** — `View > Display` (Full App) and the right-click /
  tray menus list every display, e.g. `Display 1 — 1920x1080 (primary)`,
  plus `Primary display` and `Follow mouse`. The choice is saved in
  `~/.config/synced-lyrics-linux/config.json`. The Overlay moves to the
  chosen display immediately; the Full App moves via
  `View > Move Full App to selected display`. A saved display that has
  disappeared falls back to primary.
- **Any MPRIS player** — Spotify desktop, YouTube Music in Firefox /
  Chromium, VLC, Elisa… discovered automatically, no player names
  hardcoded. Player selector dropdown when several are playing.
- **Synced lyrics from LRCLIB** — free, no key. Line-level sync, with
  plain-lyrics and instrumental fallbacks, and a disk cache in
  `~/.cache/synced-lyrics-linux/`.
- **Demo mode** — `--demo` plays a fake Coldplay — Yellow with sample
  lyrics, so you can try the UI with no player running.

## Install

### CachyOS / Arch Linux (one-command path)

```bash
git clone https://github.com/konn-neko/synced-lyrics-linux.git
cd synced-lyrics-linux
chmod +x install.sh
./install.sh
```

> GitHub's web upload doesn't preserve the executable bit, so a fresh
> clone may report `./install.sh exists but is not an executable file`
> (fish) / `Permission denied` (bash). The `chmod +x install.sh` line
> above fixes that. `bash install.sh` also works without it.

`install.sh` installs `pyside6` + `playerctl` via pacman (with
sudo — run it yourself), then creates a `synced-lyrics` launcher in
`~/.local/bin/` and a `synced-lyrics-linux.desktop` file so the app
appears in the KDE application launcher.

### Ubuntu / Debian

```bash
git clone https://github.com/konn-neko/synced-lyrics-linux.git
cd synced-lyrics-linux
sudo apt-get install python3-pyside6 playerctl
python3 synced_lyrics.py
```

### Fedora

```bash
git clone https://github.com/konn-neko/synced-lyrics-linux.git
cd synced-lyrics-linux
sudo dnf install python3-pyside6 playerctl
python3 synced_lyrics.py
```

### Generic (pip venv, any distro)

```bash
git clone https://github.com/konn-neko/synced-lyrics-linux.git
cd synced-lyrics-linux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python synced_lyrics.py
```

Requires Python 3.10+ and Qt6 (via PySide6). `playerctl` is optional —
it's the fallback if QtDBus can't reach a player.

## Run

```bash
synced-lyrics                 # Full App (after install.sh)
synced-lyrics --mode overlay  # start straight in Overlay mode
synced-lyrics --demo          # try the UI with no player running
```

Or without installing: `python3 synced_lyrics.py` from the repo folder.
Play something in Spotify or YouTube Music and the lyrics follow.
If several players are running, pick one in the Player dropdown.

## CLI flags

| Flag | What it does |
|---|---|
| `--mode full \| overlay` | Start in Full App (default) or Overlay mode |
| `--screen N` | Launch on display N (0-based; see View > Display) |
| `--demo` | Demo mode: fake Coldplay — Yellow, sample lyrics, no player needed |
| `--test-lyrics "Artist" "Title"` | Fetch lyrics from LRCLIB and print the LRC, then exit |
| `--help` | Show all flags |

Examples:

```bash
synced-lyrics --mode overlay --screen 1
synced-lyrics --test-lyrics "Coldplay" "Yellow"
```

## How it works

```
Spotify / YouTube Music / VLC
        │  MPRIS over D-Bus (title, artist, album, duration, position)
        ▼
PlaybackEngine  (one poller, 250 ms, position interpolated
        │       with a monotonic clock between polls)
        ├─► Better Lyrics  GET /getLyrics?s=&a=&al=&d=  (TTML, word sync)
        │           (cache-first, no key; 401/429 → fall back)
        ├─► QQ Music  GET /qq/getLyrics  (QRC, word sync)
        ├─► LRCLIB  GET /api/get?artist_name=&track_name=&album_name=&duration=
        │           (fallback /api/search; cached in ~/.cache/synced-lyrics-linux/)
        └─► Kugou  GET /kugou/getLyrics  (LRC, line sync, last resort)
        ▼
Full App window  ⇄  Overlay window  ⇄  System tray
        (all three render the same engine state)
```

- **MPRIS:** the standard Linux media-player interface. Primary path is
  `PySide6.QtDBus`; if that fails, `playerctl` is used. Browsers report
  position in 1-second steps, so the engine interpolates between polls
  with a monotonic anchor — a floored report can never drag the estimate
  backwards, so the current line can't flip back and forth.
- **Better Lyrics:** `https://api.betterlyrics.org/getLyrics` returns
  `{"ttml": "..."}` — TTML with a start/end time for every word, which
  is what powers word-by-word highlighting. Cache-first and free: songs
  already cached need no API key at all, and this app never sends one.
  A cache miss without a key returns 401 (keys aren't being issued
  right now) — the app just falls back to the next provider instead
  of failing the track. The QQ provider (`/qq/getLyrics`) returns
  QRC, which is also word-level (each word carries its own
  start/duration in ms); the Kugou provider (`/kugou/getLyrics`)
  returns line-level LRC only and is the last resort. Docs:
  https://lyrics-api-docs.boidu.dev
- **LRCLIB:** `https://lrclib.net/api/get` returns `syncedLyrics` (LRC),
  `plainLyrics`, `instrumental`, `duration`. No key; the client sends
  `Lrclib-Client: synced-lyrics-linux/1.0`. Rate limits (~45 req/min
  generic, ~90/min identified) are honoured — a 429 shows
  "rate limited — retry in Ns" instead of hammering the API.
- **Lyric parsing:** TTML `<p>` lines / `<span>` words (whitespace
  between spans is the space between words — never pretty-printed);
  LRC `[mm:ss.xx]` / `[mm:ss]`, multiple timestamps per line,
  `[offset:]`, and enhanced `<mm:ss.xx>` word tags (parsed into
  per-word timings, e.g. from a BetterLyrics `.lrc` export). See
  [AGENTS.md](AGENTS.md) for the full rules.

Full data-flow diagram: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Lyrics sources

| Source | Sync level | Key? | When it's used |
|---|---|---|---|
| Better Lyrics API (`api.betterlyrics.org`) | Word / syllable (TTML) | No — cache-first, cached songs are free | First, for every track |
| QQ Music via Better Lyrics (`/qq/getLyrics`) | Word (QRC) | No — cache-first | Second, when TTML has no match |
| LRCLIB (`lrclib.net`) | Line (LRC); some records word-tagged | No | Third, when TTML and QQ have no match |
| Kugou via Better Lyrics (`/kugou/getLyrics`) | Line (LRC) | No — cache-first | Last resort, when nothing else matched |

The picker (`Lyrics > Choose lyrics…`) pulls all four at once and
lists every match, word-synced first.

The status bar always names the real source, e.g. "Word-synced lyrics
from Better Lyrics (38 lines)" or "Synced lyrics from LRCLIB (37
lines)", with `[cached]` when the lyrics came from the disk cache
(`~/.cache/synced-lyrics-linux/`, `bl-` prefix for Better Lyrics
entries). Only artist/title/album/duration are ever sent to either
service. Lyrics are fetched at runtime and never shipped with the repo.

## Limitations

- **Lyrics coverage depends on both providers.** Not every track has
  synced lyrics; some have plain lyrics only, some have none. The app
  says so plainly ("No lyrics found") instead of guessing.
- **Word sync needs word timings.** Only tracks with TTML word data
  (Better Lyrics) or Enhanced-LRC word tags get word-by-word
  highlighting; everything else is line-only, which still works fine.
- **Sync can be off by a second or two** on some tracks — use the
  Sync `−0.5s` / `+0.5s` buttons; the offset is saved.
- **Browsers report position in 1 s steps** — the app interpolates, so
  the highlight still moves smoothly, but a seek in the browser can
  take up to a poll (~250 ms) to show up.
- **Overlay click-through** needs the tray menu or the `◉` handle to
  turn back off (the overlay itself ignores clicks in that mode).
- **Wayland:** window placement is the compositor's call; the Display
  picker moves the window to the chosen screen's geometry, but a
  compositor may still override always-on-top behaviour.

## Troubleshooting

| Problem | Fix |
|---|---|
| "No MPRIS player found" | Start Spotify / YouTube Music and press play once. Check the player actually exposes MPRIS: `playerctl -l` should list it. |
| `./install.sh` is not executable / Permission denied | Run `chmod +x install.sh` first, then `./install.sh` — or just `bash install.sh`. A fresh clone loses the executable bit. |
| `synced-lyrics` command not found | `install.sh` never ran (see above) — it creates the `~/.local/bin/synced-lyrics` launcher. Run it, then open a new shell so `~/.local/bin` is on your `PATH`. |
| Lyrics never load | Check network to `api.betterlyrics.org` and `lrclib.net`, and try `--test-lyrics "Coldplay" "Yellow"` — if that works, the track just isn't in either source. |
| Wrong lyrics for a track | LRCLIB matched by title/artist/duration. Use the manual search box (`Artist — Title`) to try a different spelling. |
| Multiple identical ♪ icons in the panel | Old instances from before the single-instance guard: quit them (right-click each icon → Quit) or `pkill -f synced_lyrics.py`, then launch once — a second launch now just raises the running window instead of starting another copy. |
| Overlay won't come back | Right-click the tray icon → Show Full App, or `Ctrl+1` in the Full App. If click-through is on, click the `◉` handle. |
| App doesn't start | `python3 synced_lyrics.py --demo` — if that works, the problem is MPRIS/player detection, not the app. Run `python3 -m py_compile synced_lyrics.py lyrics.py mpris.py playback.py` to check for syntax errors. |
| Tray icon missing | Some Wayland sessions hide tray icons; the app still works from the Full App window. This is a tray icon, not a plasmoid — see `docs/ARCHITECTURE.md`. |

## Configuration

`~/.config/synced-lyrics-linux/config.json` — sync offset, font size,
display choice, minimise-to-tray, overlay opacity / click-through,
`line_animations` (smooth scroll + crossfades on line change, default
`true`; set `false` for the instant old-style swap), last player. Edit
it while the app is closed, or use the in-app controls.

Lyrics cache: `~/.cache/synced-lyrics-linux/` (JSON, one file per track).
Album art cache: `~/.cache/synced-lyrics-linux/art/`. Both are gitignored
and never committed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version: run the tests,
keep the MPRIS + LRCLIB engine shared between modes, don't add logins
or API keys.

## License

MIT — see [LICENSE](LICENSE). Copyright William / konn-neko, 2026.
Lyrics themselves are fetched at runtime from Better Lyrics and LRCLIB
and are not shipped with this repo.
