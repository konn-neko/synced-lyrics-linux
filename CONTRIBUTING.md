# Contributing to synced-lyrics-linux

Repo: https://github.com/konn-neko/synced-lyrics-linux

Thanks for helping! This is a small, focused app — keep it that way.

## How to run

```bash
# CachyOS / Arch (preferred)
./install.sh
synced-lyrics --demo

# Any distro, no install
python3 synced_lyrics.py --demo
```

`--demo` needs no player and no network. Play something in Spotify or
YouTube Music to test the real MPRIS + LRCLIB path.

## How to test

```bash
python3 -m py_compile synced_lyrics.py koneko_lyrics.py lyrics.py mpris.py playback.py test_lyrics.py
python3 test_lyrics.py
QT_QPA_PLATFORM=offscreen timeout 8 python3 synced_lyrics.py --demo
QT_QPA_PLATFORM=offscreen timeout 8 python3 synced_lyrics.py --mode overlay --demo
python3 synced_lyrics.py --test-lyrics "Coldplay" "Yellow"
```

The live LRCLIB test in `test_lyrics.py` skips gracefully when offline —
don't make the suite depend on network.

## Submitting a PR

1. Fork, branch off `main`, keep the change small and focused.
2. Run the tests above and mention the results in the PR.
3. Keep the architecture: one `PlaybackEngine`, shared by Full App,
   Overlay and tray. Don't add a second MPRIS poller.
4. No logins, no API keys, no new runtime dependencies without
   discussing it first. Prefer keyless providers (see AGENTS.md for
   how to add a lyrics provider).
5. Don't commit cached lyrics, screenshots of other people's lyrics
   beyond a line or two, or anything under `~/.cache/`.
6. Update README.md / AGENTS.md / docs/ARCHITECTURE.md if behaviour
   or flags change.

## Style

- Python 3.10+, type hints where cheap, stdlib for HTTP (`urllib`).
- Qt6 via PySide6 only. No Electron, no webview.
- Plain, direct commit messages.
