"""UI smoke test — run headless: QT_QPA_PLATFORM=offscreen python3 test_ui_smoke.py

Regression test for the startup crash on real desktops:
  TypeError: QMenu.__init__ called with wrong argument types:
  QMenu.__init__(str, QApplication)

build_display_menu() was called from the tray menu with the QApplication
as parent. QMenu's parent must be a QWidget (or None), not a QApplication.
Headless runs never caught it because _build_tray() returns early when
no system tray is available, so the tray menu was never built offscreen.
This test calls build_display_menu() directly with a QApplication parent.

Also covers the line-change animation helpers (SmoothScroller,
LabelFader, _lerp_color) added so line changes glide instead of
snapping: the Full App / Overlay windows themselves are heavy to
construct headlessly, so the helpers are tested directly and the
--demo smoke run exercises the windows end to end.
"""
import os
import re
import sys
import tempfile
import time as _time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEventLoop, QLockFile, Qt, QTimer  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QLabel, QListWidget, QListWidgetItem,
    QScrollBar, QTextEdit, QWidget,
)

from lyrics import LyricLine, LyricWord, LyricsResult  # noqa: E402
from playback import MonotonicPosition, PlaybackEngine  # noqa: E402
from synced_lyrics import (  # noqa: E402
    DEFAULT_CONFIG,
    GlowWordLabel,
    LabelFader,
    LyricsItemDelegate,
    SmoothScroller,
    _lerp_color,
    _lerp_hex,
    _sweep_front,
    build_display_menu,
    build_lyrics_debug_text,
    render_line_html,
)


def _pump(app, ms):
    """Run the event loop for ms milliseconds (lets animations advance)."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_build_display_menu(app):
    picked = []

    # The crashing case: QApplication as parent.
    menu = build_display_menu(app, app, "primary", picked.append)
    assert menu is not None
    assert len(menu.actions()) >= 2, menu.actions()
    assert menu.title() == "Display"

    # QWidget parent (Full App / Overlay call sites) still works.
    w = QWidget()
    menu2 = build_display_menu(w, app, 0, picked.append)
    assert len(menu2.actions()) >= 2

    # None parent works too.
    menu3 = build_display_menu(None, app, "follow", picked.append)
    assert len(menu3.actions()) >= 2

    print("  build_display_menu: OK (QApplication, QWidget and None parents)")


def test_lerp_color():
    dim = QColor("#8b949e")
    crimson = QColor("#FF1A47")
    assert _lerp_color(dim, crimson, 0.0) == dim
    assert _lerp_color(dim, crimson, 1.0) == crimson
    mid = _lerp_color(dim, crimson, 0.5)
    assert dim.red() < mid.red() < crimson.red()
    assert crimson.green() < mid.green() < dim.green()
    print("  _lerp_color: OK (endpoints + midpoint)")


def test_smooth_scroller(app):
    bar = QScrollBar()
    bar.setRange(0, 1000)
    bar.setValue(0)
    scroller = SmoothScroller(bar)

    # Glides toward the target instead of jumping: part-way there
    # mid-animation, exactly on target after it finishes.
    scroller.scroll_to(500, enabled=True)
    _pump(app, 100)
    assert 0 < bar.value() < 500, bar.value()
    _pump(app, 400)
    assert bar.value() == 500, bar.value()

    # Retarget mid-glide: stop the old animation, start from the
    # current value, land on the NEW target (never the stale one).
    scroller.scroll_to(900, enabled=True)
    _pump(app, 60)
    assert bar.value() > 500, bar.value()  # was heading for 900
    scroller.scroll_to(100, enabled=True)
    _pump(app, 500)
    assert bar.value() == 100, bar.value()

    # Animations off: instant jump, exactly like the old behaviour.
    scroller.scroll_to(700, enabled=False)
    assert bar.value() == 700, bar.value()

    print("  SmoothScroller: OK (glide, mid-glide retarget, instant when off)")


def test_label_fader(app):
    label = QLabel("old")
    fader = LabelFader(label)

    # Fade out (~150ms), swap text, fade in (~250ms).
    fader.show_text("new", enabled=True)
    _pump(app, 200)
    assert label.text() == "new", label.text()  # swapped after fade-out
    _pump(app, 500)
    assert label.text() == "new"
    assert fader.effect.opacity() == 1.0, fader.effect.opacity()
    assert fader._state == "idle"

    # New line mid-fade: must land on the NEWEST text, never stale.
    fader.show_text("A", enabled=True)
    _pump(app, 50)  # still fading out "new"
    fader.show_text("B", enabled=True)
    _pump(app, 800)
    assert label.text() == "B", label.text()
    assert fader.effect.opacity() == 1.0
    assert fader._state == "idle"

    # Same text while idle: no-op, no flicker.
    fader.show_text("B", enabled=True)
    _pump(app, 100)
    assert label.text() == "B"
    assert fader.effect.opacity() == 1.0

    # Animations off: instant swap, exactly like the old behaviour.
    fader.show_text("C", enabled=False)
    assert label.text() == "C"
    assert fader.effect.opacity() == 1.0

    print("  LabelFader: OK (crossfade, mid-fade retarget, instant when off)")


def test_config_default():
    assert DEFAULT_CONFIG.get("line_animations") is True
    print("  config: OK (line_animations defaults to True)")


def test_single_instance_lock():
    """QLockFile: a second lock on the same path fails while the first
    is held, and succeeds again after the first is released.

    Regression: without a lock, every `synced-lyrics` launch started
    another live process (close only minimises to tray), piling up
    identical ♪ icons in the KDE panel.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "synced-lyrics.lock")
        lock1 = QLockFile(path)
        assert lock1.tryLock(0), "first lock should succeed"
        lock2 = QLockFile(path)
        assert not lock2.tryLock(0), "second lock must fail while first is held"
        lock1.unlock()
        assert lock2.tryLock(0), "lock must succeed after the first is released"
        lock2.unlock()
    print("  single-instance lock: OK (second tryLock fails, succeeds after release)")


def test_local_server_raise(app):
    """A QLocalSocket message reaches a listening QLocalServer.

    This is the raise-existing mechanism: a second launch connects
    to the first instance's server, which raises its Full App window.
    """
    name = "synced-lyrics-linux-test-raise"
    QLocalServer.removeServer(name)
    server = QLocalServer()
    assert server.listen(name), "server should listen"
    try:
        received = []
        server.newConnection.connect(
            lambda: received.append(server.nextPendingConnection()))
        sock = QLocalSocket()
        sock.connectToServer(name)
        assert sock.waitForConnected(1000), "client should connect"
        sock.write(b"show")
        assert sock.waitForBytesWritten(1000), "client should write"
        _pump(app, 200)
        assert received, "server should have a pending connection"
        assert received[0] is not None
    finally:
        server.close()
        QLocalServer.removeServer(name)
    print("  local-server raise: OK (socket message reaches the server)")


def test_render_line_html():
    line = LyricLine(0.0, "Look at the stars", [
        LyricWord(0.0, 1.0, "Look"), LyricWord(1.0, 2.0, "at"),
        LyricWord(2.0, 3.0, "the"), LyricWord(3.0, 4.0, "stars")])
    # No words sung yet: all dim, none bold.
    html0 = render_line_html(line, -1)
    assert "Look" in html0 and "#8b949e" in html0
    assert "font-weight:bold" not in html0
    # First word sung: only it is crimson + bold.
    html1 = render_line_html(line, 0)
    assert html1.count("#FF1A47") == 1, html1
    assert html1.count("font-weight:bold") == 1, html1
    # Mid-line: words 0..1 sung, rest dim.
    html2 = render_line_html(line, 1)
    assert html2.count("#FF1A47") == 2, html2
    # Last word: everything sung.
    html3 = render_line_html(line, 3)
    assert "#8b949e" not in html3, html3
    # No word timings: plain text, HTML-escaped, exactly line-only mode.
    plain = LyricLine(0.0, "Rock & roll <3")
    assert render_line_html(plain, -1) == "Rock &amp; roll &lt;3"
    # Background words render italic.
    bg = LyricLine(0.0, "Lead echo", [
        LyricWord(0.0, 1.0, "Lead"),
        LyricWord(1.0, 2.0, "echo", is_background=True)])
    assert "font-style:italic" in render_line_html(bg, 1)
    print("  render_line_html: OK (none / first / mid / last word, plain fallback, bg italic)")


def test_monotonic_position():
    """Browser-style floored reports must never drag the estimate back.

    Flat reports at 30.0 re-adopt each poll (within the 0.5 s
    tolerance), so the estimate sits at the report — never below it,
    never decreasing. A report behind the estimate by more than the
    tolerance is stale/floored and ignored, so the estimate keeps
    interpolating forward from the anchor instead of sawtoothing.
    """
    pos = MonotonicPosition()
    t0 = 1000.0
    est = [pos.observe(30.0, t0 + i * 0.25, playing=True) for i in range(5)]
    assert est == [30.0] * 5, est
    assert all(b >= a for a, b in zip(est, est[1:]))

    pos2 = MonotonicPosition()
    pos2.observe(30.0, t0, playing=True)
    assert abs(pos2.estimate(t0 + 0.9, playing=True) - 30.9) < 1e-9
    # Stale report (30.0 is 0.9 s behind the 30.9 estimate): ignored.
    assert abs(pos2.observe(30.0, t0 + 0.9, playing=True) - 30.9) < 1e-9
    # The new second is adopted when the report jumps ahead.
    assert abs(pos2.observe(31.0, t0 + 1.0, playing=True) - 31.0) < 1e-9
    # Genuine backward seek (big jump) IS adopted...
    assert abs(pos2.observe(10.0, t0 + 1.25, playing=True) - 10.0) < 1e-9
    # ...but a report 0.65 s behind the new anchor is stale: ignored.
    assert abs(pos2.observe(9.6, t0 + 1.5, playing=True) - 10.25) < 1e-9
    assert abs(pos2.estimate(t0 + 1.75, playing=True) - 10.5) < 1e-9
    # Paused: the estimate freezes at the anchor.
    frozen = MonotonicPosition()
    frozen.observe(5.0, t0, playing=True)
    assert frozen.estimate(t0 + 10.0, playing=False) == 5.0
    print("  MonotonicPosition: OK (no sawtooth, seek back adopted, paused freezes)")


def test_engine_position_monotonic(app):
    """End to end through PlaybackEngine: floored reports, real clock.

    The engine's emitted position must never decrease and the line
    index must not flip back once it advances past a boundary.
    """
    engine = PlaybackEngine(demo=False)
    try:
        engine._timer.stop()
        engine._players_timer.stop()
        engine.lyrics = LyricsResult(artist="A", track="T", synced_lines=[
            LyricLine(0.0, "zero"), LyricLine(30.2, "thirty")])
        engine.track.status = "Playing"
        seen = []
        engine.position_changed.connect(lambda p, i: seen.append((p, i)))
        for _ in range(6):
            engine._pos.observe(30.0, _time.monotonic(), playing=True)
            engine._emit_position()
            _time.sleep(0.06)
        engine._pos.observe(31.0, _time.monotonic(), playing=True)
        engine._emit_position()
        positions = [p for p, _ in seen]
        assert all(b >= a for a, b in zip(positions, positions[1:])), positions
        idxs = [i for _, i in seen]
        assert 1 in idxs, idxs
        first_one = idxs.index(1)
        assert all(i == 1 for i in idxs[first_one:]), idxs  # never flips back
        assert positions[-1] >= 31.0, positions
    finally:
        engine.stop()
    print("  engine position: OK (emitted position monotonic, line idx never flips back)")


def _html_spans(html_text):
    """[(color, text)] for each <span> in render_line_html output."""
    return [(m.group(1), m.group(2)) for m in re.finditer(
        r'<span style="color:([^;]+);[^"]*">(.*?)</span>', html_text)]


def test_lerp_hex():
    assert _lerp_hex("#8b949e", "#FF1A47", 0.0) == "#8b949e"
    assert _lerp_hex("#8b949e", "#FF1A47", 1.0) == "#ff1a47"
    assert _lerp_hex("#8b949e", "#FF1A47", 0.5) == "#c55772"
    assert _lerp_hex("bogus", "#FF1A47", 0.25) == "bogus"
    print("  _lerp_hex: OK (endpoints, midpoint, bad input)")


def test_render_line_html_sweep():
    """Mid-word sweep + transition: the active word lights up part by
    part as the position moves through it, and the character the
    sweep front is inside gets an intermediate colour — a smooth
    gradient wash, not whole characters snapping to crimson."""
    line = LyricLine(1.05, "Virtual Insanity", [
        LyricWord(1.05, 1.71, "Virtual", parts=[
            LyricWord(1.05, 1.39, "Vir-"), LyricWord(1.39, 1.71, "tual")]),
        LyricWord(1.71, 2.14, "Insanity", parts=[
            LyricWord(1.71, 2.14, "Insanity")])])
    # pos=None (old behaviour): active word fully sung, one span per part.
    assert _html_spans(render_line_html(line, 0)) == [
        ("#FF1A47", "Vir-"), ("#FF1A47", "tual"), ("#8b949e", "Insanity")]
    # Mid first syllable (1.20, ~44% through "Vir-"): "V" sung, "i"
    # mid-transition (neither crimson nor dim), rest dim.
    spans = _html_spans(render_line_html(line, 0, 1.20))
    assert spans[0] == ("#FF1A47", "V"), spans
    assert spans[1][1] == "i" and spans[1][0] not in ("#FF1A47", "#8b949e"), spans
    assert spans[2:] == [("#8b949e", "r"), ("#8b949e", "-"),
                         ("#8b949e", "tual"), ("#8b949e", "Insanity")], spans
    # Mid second syllable (1.55, exactly half of "tual"): "t","u" sung.
    assert _html_spans(render_line_html(line, 0, 1.55)) == [
        ("#FF1A47", "Vir-"), ("#FF1A47", "t"), ("#FF1A47", "u"),
        ("#8b949e", "a"), ("#8b949e", "l"), ("#8b949e", "Insanity")]
    # Before the word starts: everything dim.
    assert all(c == "#8b949e"
               for c, _ in _html_spans(render_line_html(line, 0, 0.5)))
    # Second word active (1.80): first word fully sung (one span),
    # "I" sung, "n" mid-transition, rest dim.
    spans = _html_spans(render_line_html(line, 1, 1.80))
    assert spans[0] == ("#FF1A47", "Virtual"), spans
    assert spans[1] == ("#FF1A47", "I"), spans
    assert spans[2][1] == "n" and spans[2][0] not in ("#FF1A47", "#8b949e"), spans
    assert spans[3:] == [("#8b949e", c) for c in "sanity"], spans
    # No parts: the sweep runs across the whole word (5.0-7.0 s).
    plain = LyricLine(5.0, "stars", [LyricWord(5.0, 7.0, "stars")])
    spans = _html_spans(render_line_html(plain, 0, 6.0))
    assert spans[0] == ("#FF1A47", "s") and spans[1] == ("#FF1A47", "t"), spans
    assert spans[2][1] == "a" and spans[2][0] not in ("#FF1A47", "#8b949e"), spans
    assert spans[3:] == [("#8b949e", "r"), ("#8b949e", "s")], spans
    print("  render_line_html sweep: OK (syllable split, gradient wash, no-parts word)")


def test_lyrics_debug_text(app):
    """Help > Lyrics debug text: track, source, raw file, request log."""
    from synced_lyrics import AppController, LyricsDebugDialog
    controller = AppController(app, demo=True, mode="full", screen_choice=None)
    try:
        engine = controller.engine
        engine._timer.stop()
        engine._players_timer.stop()
        text = build_lyrics_debug_text(engine)
        assert "Coldplay" in text and "Yellow" in text, text
        assert "Lyrics source: demo" in text, text
        assert "=== Raw lyrics file (lrc) ===" in text, text
        assert "[00:05.00] Look at the stars" in text, text
        assert "=== HTTP requests (this run) ===" in text, text
        # The dialog itself builds without crashing and shows the text.
        dlg = LyricsDebugDialog(controller.full, engine)
        shown = dlg.findChild(QTextEdit).toPlainText()
        assert "Raw lyrics file" in shown and "Coldplay" in shown
    finally:
        engine.stop()
        controller.full.hide()
        controller.overlay.hide()
    print("  lyrics debug text: OK (track, source, raw file, request log, dialog builds)")


def test_sweep_front():
    """Float character position of the sweep front in the line text."""
    line = LyricLine(1.05, "Vir-tual Insanity", [
        LyricWord(1.05, 1.71, "Vir-tual", parts=[
            LyricWord(1.05, 1.39, "Vir-"), LyricWord(1.39, 1.71, "tual")]),
        LyricWord(1.71, 2.14, "Insanity", parts=[
            LyricWord(1.71, 2.14, "Insanity")])])
    assert _sweep_front(line, -1, 1.5) is None
    assert _sweep_front(line, 0, 0.5) == 0.0  # before the word starts
    # Mid first syllable: (1.20-1.05)/(1.39-1.05) * 4 chars = 1.7647.
    assert abs(_sweep_front(line, 0, 1.20) - 1.7647) < 0.01
    # Past the first word: front at its end (8 chars).
    assert _sweep_front(line, 0, 1.80) == 8.0
    # Mid second word: offset 9 + (1.80-1.71)/(2.14-1.71) * 8 = 10.674.
    assert abs(_sweep_front(line, 1, 1.80) - 10.674) < 0.01
    # pos=None: whole active word sung.
    assert _sweep_front(line, 0, None) == 8.0
    # No word timings: None.
    assert _sweep_front(LyricLine(0.0, "plain"), 0, 1.0) is None
    print("  _sweep_front: OK (before / mid-syllable / past word / None)")


def _count_pixels(img, pred):
    n = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            if pred(img.pixelColor(x, y)):
                n += 1
    return n


def _demo_line():
    return LyricLine(5.0, "Look at the stars", [
        LyricWord(5.0, 6.25, "Look", parts=[LyricWord(5.0, 6.25, "Look")]),
        LyricWord(6.25, 7.0, "at", parts=[LyricWord(6.25, 7.0, "at")]),
        LyricWord(7.0, 8.0, "the", parts=[LyricWord(7.0, 8.0, "the")]),
        LyricWord(8.0, 9.5, "stars", parts=[LyricWord(8.0, 9.5, "stars")])])


def _is_crimson(c):
    return c.red() > 200 and c.green() < 110 and c.blue() < 140


def _is_dim(c):
    return 100 < c.red() < 180 and 120 < c.green() < 180 and 130 < c.blue() < 190


def test_delegate_sweep_paint(app):
    """The delegate's smooth sweep paint: sung text crimson with a
    soft front, unsung text dim — no crash, both colours visible."""
    line = _demo_line()
    lst = QListWidget()
    lst.resize(500, 200)
    delegate = LyricsItemDelegate(lst)
    lst.setItemDelegate(delegate)
    lst.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    lst.setWordWrap(True)
    item = QListWidgetItem("Look at the stars")
    item.setForeground(QColor("#FF1A47"))
    lst.addItem(item)
    # Row 0 is current, front mid-word "the" (7.5 s).
    delegate.sweep_provider = lambda row: (
        {"line": line, "word_idx": 2, "pos": 7.5} if row == 0 else None)
    lst.show()
    _pump(app, 150)
    img = lst.viewport().grab().toImage()
    assert _count_pixels(img, _is_crimson) > 0, "no crimson (sung) pixels painted"
    assert _count_pixels(img, _is_dim) > 0, "no dim (unsung) pixels painted"
    print("  delegate sweep paint: OK (crimson + dim visible, soft front)")


def test_glow_word_label(app):
    """GlowWordLabel paints its word HTML with a crimson glow, and
    the smooth sweep path when a provider is set."""
    line = _demo_line()
    label = GlowWordLabel()
    label.resize(500, 80)
    label.setStyleSheet("color:#FF1A47; font-size:30px; font-weight:bold;")
    label.setAlignment(Qt.AlignCenter)
    label.setText(render_line_html(line, 2, 7.5,
                                   sung_color="#FF1A47", unsung_color="#c9d1d9"))
    label.show()
    _pump(app, 150)
    img = label.grab().toImage()
    assert _count_pixels(img, _is_crimson) > 0, "no crimson pixels (HTML path)"
    # Smooth sweep path via provider. Unsung here is the overlay's
    # light grey #c9d1d9, not the Full App dim.
    label.sweep_provider = lambda: {"line": line, "word_idx": 2, "pos": 7.5}
    label.update()
    _pump(app, 150)
    img2 = label.grab().toImage()
    assert _count_pixels(img2, _is_crimson) > 0, "no crimson pixels (sweep path)"
    assert _count_pixels(img2, lambda c: c.red() > 180 and c.green() > 190
                         and c.blue() > 200) > 0, "no light-grey pixels (sweep path)"
    print("  glow word label: OK (HTML glow + sweep paint)")


def test_choose_lyrics_dialog(app):
    """Picker: canned multi-provider candidates populate the list
    word-synced first (TTML, QQ, LRCLIB); picking one applies it to
    the engine and caches it under the track."""
    import mpris  # noqa: F401  (kept for symmetry with the refetch test)
    import playback
    from lyrics import LyricsResult
    from synced_lyrics import ChooseLyricsDialog

    def _candidate(source, track, album, word):
        line = LyricLine(5.0, "Look at the stars",
                         words=[LyricWord(5.0, 6.0, "Look")] if word else [])
        return LyricsResult(
            artist="Coldplay", track=track, album=album, duration=269.0,
            synced_lines=[line], source=source, lrclib_id=7 if source == "search" else None,
            raw_record={"id": 7, "artistName": "Coldplay",
                        "trackName": track, "albumName": album,
                        "duration": 269.0,
                        "syncedLyrics": "[00:05.00] Look at the stars",
                        "plainLyrics": "Look at the stars"} if source == "search" else None)

    canned = [_candidate("betterlyrics", "Yellow", "Parachutes", True),
              _candidate("qq", "Yellow", "Parachutes", True),
              _candidate("search", "Yellow", "Parachutes", False),
              _candidate("search", "Yellow", "Live 2003", False)]
    orig_search = playback.search_all_candidates
    orig_cache = playback.cache_lrclib_record
    cached = {}
    playback.search_all_candidates = lambda *a, **k: list(canned)
    playback.cache_lrclib_record = lambda rec, *a, **k: cached.setdefault("path", "/tmp/fake.json")
    try:
        engine = playback.PlaybackEngine(demo=True)
        try:
            dlg = ChooseLyricsDialog(None, engine)
            dlg.show()
            for _ in range(50):  # wait for the worker's search to land
                _pump(app, 100)
                if dlg.list_widget.count() > 0:
                    break
            assert dlg.list_widget.count() == 4, (
                f"expected 4 candidates, got {dlg.list_widget.count()}")
            assert "Better Lyrics" in dlg.list_widget.item(0).text()
            assert "QQ Music" in dlg.list_widget.item(1).text()
            assert "word-synced" in dlg.list_widget.item(0).text()
            dlg.list_widget.setCurrentRow(1)
            dlg._use_selected()
            assert engine.lyrics is canned[1], "pick did not apply to the engine"
            dlg.close()
        finally:
            engine.stop()
    finally:
        playback.search_all_candidates = orig_search
        playback.cache_lrclib_record = orig_cache
    print("  choose lyrics dialog: OK (4 candidates, TTML+QQ first, pick applies)")


def test_engine_refetch_ignores_cache(app):
    """Refetch: force=True clears the cache first and re-pulls fresh."""
    import mpris
    import playback
    from lyrics import LyricsResult

    orig_bl = playback.fetch_betterlyrics
    orig_qq = playback.fetch_qq
    orig_lrc = playback.fetch_lrclib
    orig_kg = playback.fetch_kugou
    orig_clear = playback.clear_track_cache
    cleared = []
    playback.clear_track_cache = lambda *a, **k: cleared.append(True) or 1
    playback.fetch_betterlyrics = lambda *a, **k: None
    playback.fetch_qq = lambda *a, **k: None
    playback.fetch_kugou = lambda *a, **k: None
    fresh = LyricsResult(artist="Coldplay", track="Yellow", album="Parachutes",
                         duration=269.0, synced_lines=[LyricLine(5.0, "Fresh line")],
                         source="lrclib")
    playback.fetch_lrclib = lambda *a, **k: fresh
    try:
        engine = playback.PlaybackEngine(demo=False)
        try:
            engine.track = mpris.TrackInfo(
                player="test", title="Yellow", artist="Coldplay",
                album="Parachutes", duration=269.0, status="Playing")
            engine.refetch_lyrics()
            for _ in range(50):
                _pump(app, 100)
                if engine.lyrics is fresh:
                    break
            assert engine.lyrics is fresh, "refetch did not apply the fresh result"
            assert cleared, "force refetch did not clear the cache first"
        finally:
            engine.stop()
    finally:
        playback.fetch_betterlyrics = orig_bl
        playback.fetch_qq = orig_qq
        playback.fetch_lrclib = orig_lrc
        playback.fetch_kugou = orig_kg
        playback.clear_track_cache = orig_clear
    print("  engine refetch: OK (cache cleared, fresh result applied)")


def test_lyrics_delegate_and_scroll(app):
    """Regression: word HTML must RENDER, and scrolling must be per-pixel.

    The Full App showed render_line_html() tags literally on William's
    box ("<span style=...>Ooh,</span>") because QListWidget's default
    delegate draws item text as plain text — LyricsItemDelegate paints
    it with a QTextDocument instead. And the list lurched up/down
    because the scrollbar was in the default ScrollPerItem mode (value
    in whole items) while SmoothScroller computed pixel targets.
    """
    from PySide6.QtGui import QPainter, QPixmap
    lst = QListWidget()
    lst.resize(400, 300)
    lst.setItemDelegate(LyricsItemDelegate(lst))
    lst.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    lst.setWordWrap(True)
    assert lst.verticalScrollMode() == QAbstractItemView.ScrollPerPixel
    line = LyricLine(0.0, "Look at the stars", [
        LyricWord(0.0, 1.0, "Look"), LyricWord(1.0, 2.0, "at"),
        LyricWord(2.0, 3.0, "the"), LyricWord(3.0, 4.0, "stars")])
    lst.addItem(QListWidgetItem(render_line_html(line, 1)))
    lst.addItem(QListWidgetItem("plain line here"))
    lst.show()
    _pump(app, 100)
    delegate = lst.itemDelegate()
    assert isinstance(delegate, LyricsItemDelegate)

    def view_option():
        # QListWidget has no public viewOptions() in PySide6; the
        # delegate fills font/palette from the index itself.
        from PySide6.QtWidgets import QStyleOptionViewItem
        opt = QStyleOptionViewItem()
        opt.initFrom(lst)
        opt.rect = lst.viewport().rect()
        opt.widget = lst
        return opt

    idx = lst.model().index(0, 0)
    assert delegate.sizeHint(view_option(), idx).height() > 10
    # Paint the HTML row into a pixmap: must not raise, and must draw
    # something (a literal "<span..." string would also draw, so also
    # assert the painted row is not one long unwrapped line: the
    # delegate wraps at the viewport width, so a very long HTML line
    # must be taller than a single text row).
    pm = QPixmap(400, 300)
    pm.fill(QColor("white"))
    painter = QPainter(pm)
    try:
        delegate.paint(painter, view_option(), idx)
    finally:
        painter.end()
    img = pm.toImage()
    nonwhite = sum(
        1 for y in range(0, 300, 4) for x in range(0, 400, 4)
        if img.pixelColor(x, y) != QColor("white"))
    assert nonwhite > 0, "delegate painted nothing"
    long_line = LyricLine(0.0, "word " * 40, [
        LyricWord(i, i + 1, "word") for i in range(40)])
    long_item = QListWidgetItem(render_line_html(long_line, 5))
    lst.addItem(long_item)
    _pump(app, 100)
    long_idx = lst.model().index(2, 0)
    plain_idx = lst.model().index(1, 0)
    assert (delegate.sizeHint(view_option(), long_idx).height()
            > delegate.sizeHint(view_option(), plain_idx).height()), \
        "long HTML line must wrap to multiple rows"
    print("  lyrics delegate + scroll: OK (HTML paints, wraps, ScrollPerPixel)")


def test_demo_word_highlight(app):
    """End to end (headless, --demo data): the Full App's current list
    item and the Overlay's current label carry word HTML, not plain
    text, once playback passes the first words of a line."""
    from synced_lyrics import AppController
    controller = AppController(app, demo=True, mode="full", screen_choice=None)
    # The Full App must carry the fixes: HTML delegate + per-pixel scroll.
    assert isinstance(controller.full.lyrics_list.itemDelegate(), LyricsItemDelegate)
    assert (controller.full.lyrics_list.verticalScrollMode()
            == QAbstractItemView.ScrollPerPixel)
    try:
        engine = controller.engine
        # Freeze demo playback: the 250 ms poll timer would otherwise
        # advance _demo_pos during the pumps below and change which
        # words are sung mid-assertion.
        engine._timer.stop()
        engine._players_timer.stop()
        # Demo line 0 is "Look at the stars" at 5.0 s; proportional
        # word split puts word 2 ("the") active at 7.5 s.
        engine._demo_pos = 7.5
        engine._emit_position()
        _pump(app, 100)
        item = controller.full.lyrics_list.item(0)
        assert item is not None, "demo line 0 missing"
        assert "#FF1A47" in item.text(), item.text()
        assert "Look" in item.text() and "stars" in item.text()
        assert item.text().count("#FF1A47") == 3, item.text()  # 3 words sung

        controller.show_overlay()
        engine._demo_pos = 7.5
        engine._emit_position()
        _pump(app, 700)  # let the line crossfade land
        label_text = controller.overlay.current_label.text()
        assert "#FF1A47" in label_text, label_text
        assert label_text.count("#FF1A47") == 3, label_text
    finally:
        engine.stop()
        controller.full.hide()
        controller.overlay.hide()
    print("  demo word highlight: OK (Full App item + Overlay label carry word HTML)")


def main():
    app = QApplication(sys.argv)
    test_build_display_menu(app)
    test_lerp_color()
    test_smooth_scroller(app)
    test_label_fader(app)
    test_config_default()
    test_single_instance_lock()
    test_local_server_raise(app)
    test_render_line_html()
    test_lerp_hex()
    test_sweep_front()
    test_render_line_html_sweep()
    test_delegate_sweep_paint(app)
    test_glow_word_label(app)
    test_choose_lyrics_dialog(app)
    test_engine_refetch_ignores_cache(app)
    test_lyrics_debug_text(app)
    test_lyrics_delegate_and_scroll(app)
    test_monotonic_position()
    test_engine_position_monotonic(app)
    test_demo_word_highlight(app)
    print("test_ui_smoke: OK — all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
