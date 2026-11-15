#!/usr/bin/env python3
"""Backwards-compatible entry point.

The app is now synced-lyrics-linux (repo konn-neko/synced-lyrics-linux);
the real entry point is synced_lyrics.py. This shim exists so the old
`python3 koneko_lyrics.py` command from early drafts still works.
"""
from synced_lyrics import main

if __name__ == "__main__":
    raise SystemExit(main())
