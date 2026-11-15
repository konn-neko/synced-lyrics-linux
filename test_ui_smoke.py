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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QScrollBar, QWidget  # noqa: E402

from synced_lyrics import (  # noqa: E402
    DEFAULT_CONFIG,
    LabelFader,
    SmoothScroller,
    _lerp_color,
    build_display_menu,
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


def main():
    app = QApplication(sys.argv)
    test_build_display_menu(app)
    test_lerp_color()
    test_smooth_scroller(app)
    test_label_fader(app)
    test_config_default()
    print("test_ui_smoke: OK — all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
