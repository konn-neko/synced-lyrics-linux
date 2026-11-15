"""UI smoke test — run headless: QT_QPA_PLATFORM=offscreen python3 test_ui_smoke.py

Regression test for the startup crash on real desktops:
  TypeError: QMenu.__init__ called with wrong argument types:
  QMenu.__init__(str, QApplication)

build_display_menu() was called from the tray menu with the QApplication
as parent. QMenu's parent must be a QWidget (or None), not a QApplication.
Headless runs never caught it because _build_tray() returns early when
no system tray is available, so the tray menu was never built offscreen.
This test calls build_display_menu() directly with a QApplication parent.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from synced_lyrics import build_display_menu  # noqa: E402


def main():
    app = QApplication(sys.argv)
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

    print("test_ui_smoke: OK — build_display_menu works with QApplication, QWidget and None parents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
