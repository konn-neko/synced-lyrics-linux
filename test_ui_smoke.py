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
    QScrollBar, QWidget,
)

from lyrics import LyricLine, LyricWord, LyricsResult  # noqa: E402
from playback import MonotonicPosition, PlaybackEngine  # noqa: E402
from synced_lyrics import (  # noqa: E402
    DEFAULT_CONFIG,
    LabelFader,
    LyricsItemDelegate,
    SmoothScroller,
    _lerp_color,
    build_display_menu,
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
    test_lyrics_delegate_and_scroll(app)
    test_monotonic_position()
    test_engine_position_monotonic(app)
    test_demo_word_highlight(app)
    print("test_ui_smoke: OK — all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
