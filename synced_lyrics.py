#!/usr/bin/env python3
"""synced-lyrics-linux — a native Linux synced-lyrics app.

Repo: https://github.com/konn-neko/synced-lyrics-linux
A BetterLyrics-style app for Linux: reads what's playing via MPRIS
(Spotify, YouTube Music in a browser, VLC, ...), fetches time-synced
lyrics from LRCLIB (free, no key, no login), and shows them in sync.

Modes (one shared PlaybackEngine, so switching never loses sync):
  * Full App  -- a normal resizable window (Ctrl+1)
  * Overlay   -- frameless, always-on-top, translucent (Ctrl+2)
  * Tray      -- QSystemTrayIcon with playback controls

CLI:  python3 synced_lyrics.py [--mode full|overlay] [--screen N]
                                 [--demo] [--test-lyrics "Artist" "Title"]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSizeGrip, QSlider, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget,
    QWidgetAction,
)

import mpris
from lyrics import fetch_lrclib
from playback import PlaybackEngine

APP_NAME = "synced-lyrics-linux"
REPO_URL = "https://github.com/konn-neko/synced-lyrics-linux"
CONFIG_DIR = Path.home() / ".config" / "synced-lyrics-linux"
CONFIG_FILE = CONFIG_DIR / "config.json"
ART_CACHE = Path.home() / ".cache" / "synced-lyrics-linux" / "art"

# Qt6 renamed WA_TransparentForInput -> WA_TransparentForMouseEvents.
# (Qt5 name kept as fallback.)
_CLICK_THROUGH_ATTR = getattr(Qt, "WA_TransparentForMouseEvents", None) or getattr(
    Qt, "WA_TransparentForInput", None)

# William's theme: obsidian blue + luminous crimson on dark.
OBSIDIAN = "#466EA5"
CRIMSON = "#FF1A47"
BG = "#0d1117"
PANEL = "#161b22"
TEXT = "#e6edf3"
DIM = "#8b949e"

DEFAULT_CONFIG = {
    "offset": 0.0,
    "font_size": 16,
    "display": "primary",       # "primary" | "follow" | <screen index int>
    "minimise_to_tray": True,
    "overlay_opacity": 0.85,
    "overlay_click_through": False,
    "last_player": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def fmt_time(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def make_tray_icon() -> QIcon:
    """Draw a music-note icon in code -- no external asset needed."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(CRIMSON))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    p.setFont(QFont("DejaVu Sans", 34, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


# ------------------------------------------------------------------ screens
def screen_list(app: QApplication) -> list:
    return list(app.screens())


def screen_label(i: int, screen) -> str:
    g = screen.geometry()
    label = f"Display {i + 1} — {g.width()}x{g.height()}"
    if screen is app_primary(screen):
        label += " (primary)"
    return label


def app_primary(screen) -> bool:
    return screen == QGuiApplication.primaryScreen()


def resolve_screen(app: QApplication, choice) -> object:
    """choice: 'primary' | 'follow' | int index. Falls back to primary
    when the saved display has disappeared."""
    screens = screen_list(app)
    if not screens:
        return QGuiApplication.primaryScreen()
    if choice == "follow":
        try:
            from PySide6.QtGui import QCursor
            s = app.screenAt(QCursor.pos())
            return s or QGuiApplication.primaryScreen()
        except Exception:
            return QGuiApplication.primaryScreen()
    if isinstance(choice, int):
        if 0 <= choice < len(screens):
            return screens[choice]
        return QGuiApplication.primaryScreen()
    return QGuiApplication.primaryScreen()


def move_window_to_screen(window: QWidget, app: QApplication, choice) -> object:
    screen = resolve_screen(app, choice)
    if screen is None:
        return None
    geom = screen.availableGeometry()
    window.move(geom.center() - window.rect().center())
    return screen


def build_display_menu(parent, app: QApplication, current_choice, on_pick):
    """A QMenu listing every display + Primary / Follow mouse."""
    menu = QMenu("Display", parent)
    screens = screen_list(app)
    for i, scr in enumerate(screens):
        act = QAction(screen_label(i, scr), menu, checkable=True)
        act.setChecked(current_choice == i)
        act.triggered.connect(lambda _c=False, idx=i: on_pick(idx))
        menu.addAction(act)
    menu.addSeparator()
    act = QAction("Primary display", menu, checkable=True)
    act.setChecked(current_choice == "primary")
    act.triggered.connect(lambda _c=False: on_pick("primary"))
    menu.addAction(act)
    act = QAction("Follow mouse", menu, checkable=True)
    act.setChecked(current_choice == "follow")
    act.triggered.connect(lambda _c=False: on_pick("follow"))
    menu.addAction(act)
    return menu


# ------------------------------------------------------------- shared view
class LyricsViewMixin:
    """Shared helpers for the two windows: both render from the engine."""

    def _apply_lyrics(self, lyrics):
        raise NotImplementedError


# --------------------------------------------------------------- full app
class FullAppWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.engine: PlaybackEngine = controller.engine
        self.cfg = controller.cfg
        self.setWindowTitle("Synced Lyrics — Linux")
        self.setWindowIcon(make_tray_icon())
        self.resize(900, 650)
        self._dragging_slider = False
        self._current_idx = -2
        self._art_url = ""

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # --- Now Playing bar ---
        top = QHBoxLayout()
        self.art_label = QLabel("♪")
        self.art_label.setFixedSize(64, 64)
        self.art_label.setAlignment(Qt.AlignCenter)
        self.art_label.setStyleSheet(f"background:{PANEL}; border-radius:6px; font-size:28px;")
        top.addWidget(self.art_label)

        info = QVBoxLayout()
        self.title_label = QLabel("Nothing playing")
        self.title_label.setStyleSheet("font-size:18px; font-weight:bold;")
        self.artist_label = QLabel("")
        self.artist_label.setStyleSheet(f"color:{DIM}; font-size:13px;")
        info.addWidget(self.title_label)
        info.addWidget(self.artist_label)
        info.addStretch(1)
        top.addLayout(info, stretch=1)

        controls = QHBoxLayout()
        self.prev_btn = QPushButton("⏮")
        self.play_btn = QPushButton("▶")
        self.next_btn = QPushButton("⏭")
        for b in (self.prev_btn, self.play_btn, self.next_btn):
            b.setFixedSize(40, 36)
            b.setStyleSheet(f"background:{PANEL}; border-radius:6px; font-size:16px;")
        self.prev_btn.clicked.connect(self.engine.previous_track)
        self.play_btn.clicked.connect(self.engine.play_pause)
        self.next_btn.clicked.connect(self.engine.next_track)
        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_btn)
        top.addLayout(controls)
        layout.addLayout(top)

        # --- Progress ---
        prog = QHBoxLayout()
        self.elapsed_label = QLabel("0:00")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging_slider", True))
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.total_label = QLabel("0:00")
        prog.addWidget(self.elapsed_label)
        prog.addWidget(self.slider, stretch=1)
        prog.addWidget(self.total_label)
        layout.addLayout(prog)

        # --- Player / offset / font / pop-out row ---
        row = QHBoxLayout()
        row.addWidget(QLabel("Player:"))
        self.player_combo = QComboBox()
        self.player_combo.currentTextChanged.connect(self._on_player_chosen)
        row.addWidget(self.player_combo, stretch=1)
        row.addWidget(QLabel("Sync:"))
        self.offset_minus = QPushButton("−0.5s")
        self.offset_plus = QPushButton("+0.5s")
        self.offset_label = QLabel("0.0s")
        self.offset_minus.clicked.connect(lambda: self._change_offset(-0.5))
        self.offset_plus.clicked.connect(lambda: self._change_offset(0.5))
        row.addWidget(self.offset_minus)
        row.addWidget(self.offset_label)
        row.addWidget(self.offset_plus)
        row.addWidget(QLabel("Font:"))
        self.font_minus = QPushButton("A−")
        self.font_plus = QPushButton("A+")
        self.font_minus.clicked.connect(lambda: self._change_font(-1))
        self.font_plus.clicked.connect(lambda: self._change_font(1))
        row.addWidget(self.font_minus)
        row.addWidget(self.font_plus)
        self.popout_btn = QPushButton("Pop out overlay  ⧉")
        self.popout_btn.clicked.connect(controller.show_overlay)
        row.addWidget(self.popout_btn)
        layout.addLayout(row)
        self._refresh_offset_label()

        # --- Manual search ---
        search_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Artist — Title  (manual lookup when auto-match fails)")
        self.search_box.returnPressed.connect(self._on_manual_search)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._on_manual_search)
        search_row.addWidget(self.search_box, stretch=1)
        search_row.addWidget(search_btn)
        layout.addLayout(search_row)

        # --- Lyrics pane ---
        self.lyrics_list = QListWidget()
        self.lyrics_list.setStyleSheet(
            f"background:{BG}; border:1px solid {PANEL}; border-radius:6px;")
        self.lyrics_list.itemClicked.connect(self._on_line_clicked)
        layout.addWidget(self.lyrics_list, stretch=1)

        self.statusBar().showMessage("Starting…")
        self._build_menu()
        self._apply_style()

        # Engine signals -- single poller, this window just renders.
        self.engine.track_changed.connect(self._on_track)
        self.engine.lyrics_changed.connect(self._on_lyrics)
        self.engine.status_changed.connect(self.statusBar().showMessage)
        self.engine.position_changed.connect(self._on_position)
        self.engine.players_changed.connect(self._on_players)
        self._on_players(self.engine._players)
        if self.engine.track.title:
            self._on_track(self.engine.track)
        if self.engine.lyrics is not None:
            self._on_lyrics(self.engine.lyrics)

    def _apply_style(self):
        self.setStyleSheet(
            f"QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; }}"
            f"QListWidget {{ font-size:{self.cfg['font_size']}px; }}"
            f"QPushButton {{ background:{PANEL}; border-radius:6px; padding:4px 8px; }}"
            f"QPushButton:hover {{ background:#21262d; }}"
            f"QLineEdit, QComboBox {{ background:{PANEL}; border:1px solid #30363d;"
            f" border-radius:6px; padding:5px; }}")

    def _build_menu(self):
        c = self.controller
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        a = QAction("Show Overlay  (Ctrl+2)", self)
        a.setShortcut("Ctrl+2")
        a.triggered.connect(c.show_overlay)
        file_menu.addAction(a)
        a = QAction("Show Full App  (Ctrl+1)", self)
        a.setShortcut("Ctrl+1")
        a.triggered.connect(c.show_full)
        file_menu.addAction(a)
        file_menu.addSeparator()
        self.min_tray_act = QAction("Minimise to tray on close", self, checkable=True)
        self.min_tray_act.setChecked(bool(self.cfg.get("minimise_to_tray", True)))
        self.min_tray_act.triggered.connect(c.toggle_minimise_to_tray)
        file_menu.addAction(self.min_tray_act)
        a = QAction("Quit", self)
        a.triggered.connect(c.quit_app)
        file_menu.addAction(a)

        view_menu = menubar.addMenu("&View")
        view_menu.addMenu(build_display_menu(
            self, QApplication.instance(), self.cfg.get("display", "primary"),
            c.pick_display))
        a = QAction("Move Full App to selected display", self)
        a.triggered.connect(lambda: move_window_to_screen(
            self, QApplication.instance(), self.cfg.get("display", "primary")))
        view_menu.addAction(a)

        help_menu = menubar.addMenu("&Help")
        a = QAction("About", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, "Synced Lyrics — Linux",
            "synced-lyrics-linux\n\nNative Linux synced lyrics.\n"
            "MPRIS + LRCLIB, no login, no API key.\n\n" + REPO_URL))
        help_menu.addAction(a)

    # ---------------------------------------------------------- engine slots
    def _on_players(self, players):
        current = self.player_combo.currentText()
        self.player_combo.blockSignals(True)
        self.player_combo.clear()
        self.player_combo.addItems(players)
        if self.engine.player in players:
            self.player_combo.setCurrentText(self.engine.player)
        elif current in players:
            self.player_combo.setCurrentText(current)
        self.player_combo.blockSignals(False)

    def _on_player_chosen(self, name):
        if name and name != self.engine.player:
            self.engine.set_player(name)
            self.cfg["last_player"] = name
            save_config(self.cfg)

    def _on_track(self, track):
        self.title_label.setText(track.title or "Nothing playing")
        self.artist_label.setText(
            f"{track.artist}  •  {track.album}" if track.artist else "")
        self.total_label.setText(fmt_time(track.duration))
        self.play_btn.setText("⏸" if track.status == "Playing" else "▶")
        if track.art_url and track.art_url != self._art_url:
            self._art_url = track.art_url
            self._load_art(track.art_url)

    def _load_art(self, url):
        try:
            key = hashlib.sha256(url.encode()).hexdigest()[:16]
            ART_CACHE.mkdir(parents=True, exist_ok=True)
            path = ART_CACHE / key
            if not path.exists():
                req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
                with urllib.request.urlopen(req, timeout=10) as r:
                    path.write_bytes(r.read())
            from PySide6.QtGui import QPixmap as _PM
            pm = _PM(str(path))
            if not pm.isNull():
                self.art_label.setPixmap(pm.scaled(64, 64, Qt.KeepAspectRatio,
                                                   Qt.SmoothTransformation))
                self.art_label.setText("")
        except Exception:
            pass  # art is decorative; never break playback for it

    def _on_lyrics(self, lyrics):
        self.lyrics_list.clear()
        self._current_idx = -2
        if lyrics is None:
            item = QListWidgetItem("No lyrics for this track — try the manual search above.")
            item.setForeground(QColor(DIM))
            self.lyrics_list.addItem(item)
            return
        if lyrics.instrumental:
            item = QListWidgetItem("♪  Instrumental — no lyrics  ♪")
            item.setForeground(QColor(DIM))
            self.lyrics_list.addItem(item)
            return
        if not lyrics.has_synced and lyrics.plain_lyrics:
            for line in lyrics.plain_lyrics.splitlines():
                if line.strip():
                    self.lyrics_list.addItem(QListWidgetItem(line.strip()))
            return
        if not lyrics.has_synced:
            item = QListWidgetItem("No synced lyrics for this track.")
            item.setForeground(QColor(DIM))
            self.lyrics_list.addItem(item)
            return
        for line in lyrics.synced_lines:
            item = QListWidgetItem(line.text)
            item.setData(Qt.UserRole, line.time)
            item.setForeground(QColor(DIM))
            self.lyrics_list.addItem(item)

    def _on_position(self, pos, idx):
        track = self.engine.track
        self.elapsed_label.setText(fmt_time(pos))
        if track.duration:
            if not self._dragging_slider:
                self.slider.setValue(int(pos / track.duration * 1000))
            self.total_label.setText(fmt_time(track.duration))
        self.play_btn.setText("⏸" if track.status == "Playing" else "▶")
        if idx != self._current_idx and self.engine.lyrics and self.engine.lyrics.has_synced:
            self._highlight(idx)

    def _highlight(self, idx):
        prev = self._current_idx
        if 0 <= prev < self.lyrics_list.count():
            item = self.lyrics_list.item(prev)
            item.setForeground(QColor(DIM))
            f = QFont(self.font())
            f.setPointSize(self.cfg["font_size"])
            f.setBold(False)
            item.setFont(f)
        self._current_idx = idx
        if 0 <= idx < self.lyrics_list.count():
            item = self.lyrics_list.item(idx)
            item.setForeground(QColor(CRIMSON))
            f = QFont(self.font())
            f.setPointSize(self.cfg["font_size"] + 2)
            f.setBold(True)
            item.setFont(f)
            self.lyrics_list.scrollToItem(item, QListWidget.PositionAtCenter)

    def _on_slider_released(self):
        self._dragging_slider = False
        if self.engine.track.duration:
            self.engine.seek(self.slider.value() / 1000 * self.engine.track.duration)

    def _on_line_clicked(self, item):
        t = item.data(Qt.UserRole)
        if t is not None:
            self.engine.seek(float(t))

    def _on_manual_search(self):
        text = self.search_box.text().strip()
        if "—" in text:
            artist, title = [p.strip() for p in text.split("—", 1)]
        elif " - " in text:
            artist, title = [p.strip() for p in text.split(" - ", 1)]
        else:
            QMessageBox.information(self, "Search", "Type:  Artist — Title")
            return
        if artist and title:
            self.engine.search_manual(artist, title)

    def _change_offset(self, delta):
        self.cfg["offset"] = round(self.cfg.get("offset", 0.0) + delta, 1)
        self.engine.set_offset(self.cfg["offset"])
        self._refresh_offset_label()
        save_config(self.cfg)

    def _refresh_offset_label(self):
        self.offset_label.setText(f"{self.cfg.get('offset', 0.0):+.1f}s")

    def _change_font(self, delta):
        self.cfg["font_size"] = max(10, min(32, self.cfg.get("font_size", 16) + delta))
        self._apply_style()
        save_config(self.cfg)
        # re-highlight so the current line picks up the new size
        idx = self._current_idx
        self._current_idx = -2
        if idx >= 0:
            self._highlight(idx)

    def closeEvent(self, event):
        if self.controller.cfg.get("minimise_to_tray", True) and self.controller.tray:
            event.ignore()
            self.hide()
            self.controller.tray.showMessage(
                APP_NAME, "Still running in the tray — right-click the tray icon to quit.",
                QSystemTrayIcon.Information, 3000)
        else:
            event.accept()


# ------------------------------------------------------------ overlay window
class ClickThroughHandle(QWidget):
    """Tiny floating handle shown while the overlay is click-through.

    The overlay itself ignores all input in that mode, so this separate
    top-level widget is the only way back: click it to turn click-through off.
    """

    def __init__(self, controller):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.controller = controller
        self.setFixedSize(28, 28)
        label = QLabel("◉", self)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f"background:{CRIMSON}; color:white; border-radius:14px; font-size:16px;")
        label.setGeometry(0, 0, 28, 28)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setToolTip("Click to turn overlay click-through OFF")

    def mousePressEvent(self, event):
        self.controller.set_overlay_click_through(False)
        event.accept()


class OverlayWindow(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.controller = controller
        self.engine: PlaybackEngine = controller.engine
        self.cfg = controller.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Synced Lyrics — Overlay")
        self.resize(640, 190)
        self._drag_pos = None
        self._current_idx = -2
        self._next_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)

        top = QHBoxLayout()
        self.track_label = QLabel("Nothing playing")
        self.track_label.setStyleSheet("font-size:12px; color:#c9d1d9;")
        top.addWidget(self.track_label, stretch=1)
        self.play_btn = QPushButton("▶")
        self.min_btn = QPushButton("–")
        self.close_btn = QPushButton("✕")
        for b in (self.play_btn, self.min_btn, self.close_btn):
            b.setFixedSize(28, 28)
            b.setStyleSheet("background:rgba(255,255,255,0.12); color:white; border-radius:6px;")
        self.play_btn.clicked.connect(self.engine.play_pause)
        self.min_btn.clicked.connect(controller.show_full)
        self.close_btn.clicked.connect(self.hide)
        top.addWidget(self.play_btn)
        top.addWidget(self.min_btn)
        top.addWidget(self.close_btn)
        layout.addLayout(top)

        self.current_label = QLabel("♪")
        self.current_label.setWordWrap(True)
        self.current_label.setAlignment(Qt.AlignCenter)
        self.current_label.setStyleSheet(f"color:{CRIMSON}; font-size:30px; font-weight:bold;")
        layout.addWidget(self.current_label, stretch=1)

        self.next_label = QLabel("")
        self.next_label.setWordWrap(True)
        self.next_label.setAlignment(Qt.AlignCenter)
        self.next_label.setStyleSheet(f"color:{OBSIDIAN}; font-size:16px;")
        layout.addWidget(self.next_label)

        grip = QSizeGrip(self)
        layout.addWidget(grip, alignment=Qt.AlignRight)

        self.handle = ClickThroughHandle(controller)
        self._apply_opacity()
        self._apply_click_through()

        self.engine.track_changed.connect(self._on_track)
        self.engine.lyrics_changed.connect(self._on_lyrics)
        self.engine.position_changed.connect(self._on_position)

    # ------------------------------------------------------------ appearance
    def _apply_opacity(self):
        self.setWindowOpacity(float(self.cfg.get("overlay_opacity", 0.85)))

    def _apply_click_through(self):
        on = bool(self.cfg.get("overlay_click_through", False))
        if _CLICK_THROUGH_ATTR is not None:
            self.setAttribute(_CLICK_THROUGH_ATTR, on)
        if on:
            # Park the handle at the overlay's top-right corner.
            pos = self.mapToGlobal(self.rect().topRight()) - self.handle.rect().topRight()
            self.handle.move(pos)
            self.handle.show()
        else:
            self.handle.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(13, 17, 23, 220))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 12, 12)

    # ------------------------------------------------------------ interaction
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.controller.show_full()
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        c = self.controller
        menu = QMenu(self)
        a = QAction("Show Full App  (Ctrl+1)", self)
        a.triggered.connect(c.show_full)
        menu.addAction(a)
        a = QAction("Play/Pause", self)
        a.triggered.connect(self.engine.play_pause)
        menu.addAction(a)
        a = QAction("Next", self)
        a.triggered.connect(self.engine.next_track)
        menu.addAction(a)
        a = QAction("Previous", self)
        a.triggered.connect(self.engine.previous_track)
        menu.addAction(a)
        menu.addSeparator()
        menu.addMenu(build_display_menu(
            self, QApplication.instance(), self.cfg.get("display", "primary"),
            c.pick_display))
        ct = QAction("Click-through (clicks pass through)", self, checkable=True)
        ct.setChecked(bool(self.cfg.get("overlay_click_through", False)))
        ct.triggered.connect(lambda checked: c.set_overlay_click_through(checked))
        menu.addAction(ct)
        # Opacity slider inside the menu.
        slider = QSlider(Qt.Horizontal)
        slider.setRange(30, 100)
        slider.setValue(int(float(self.cfg.get("overlay_opacity", 0.85)) * 100))
        slider.valueChanged.connect(
            lambda v: c.set_overlay_opacity(v / 100.0))
        slider.setToolTip("Overlay opacity")
        action = QWidgetAction(menu)
        action.setDefaultWidget(slider)
        menu.addAction(action)
        menu.addSeparator()
        a = QAction("Hide overlay", self)
        a.triggered.connect(self.hide)
        menu.addAction(a)
        menu.exec(event.globalPos())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_1 and event.modifiers() & Qt.ControlModifier:
            self.controller.show_full()
        super().keyPressEvent(event)

    # ---------------------------------------------------------- engine slots
    def _on_track(self, track):
        self.track_label.setText(track.display)
        self.play_btn.setText("⏸" if track.status == "Playing" else "▶")

    def _on_lyrics(self, lyrics):
        self._current_idx = -2
        if lyrics is None or not lyrics.has_synced:
            self.current_label.setText("♪" if not lyrics else "No synced lyrics")
            self.next_label.setText("")
            return
        self._on_position(0.0, -1)

    def _on_position(self, pos, idx):
        lyrics = self.engine.lyrics
        track = self.engine.track
        self.play_btn.setText("⏸" if track.status == "Playing" else "▶")
        if not lyrics or not lyrics.has_synced:
            return
        if idx == self._current_idx:
            return
        self._current_idx = idx
        if idx < 0:
            self.current_label.setText("♪")
            self.next_label.setText(lyrics.synced_lines[0].text if lyrics.synced_lines else "")
        else:
            self.current_label.setText(lyrics.synced_lines[idx].text)
            nxt = lyrics.synced_lines[idx + 1].text if idx + 1 < len(lyrics.synced_lines) else ""
            self.next_label.setText(nxt)


# ------------------------------------------------------------ app controller
class AppController:
    """Owns the single PlaybackEngine and all windows/tray.

    Plain object on purpose: the windows are the QObjects. The engine is
    the only shared state -- Full App, Overlay and tray all render from it.
    """

    def __init__(self, app: QApplication, demo: bool, mode: str, screen_choice):
        self.app = app
        self.cfg = load_config()
        if screen_choice is not None:
            self.cfg["display"] = screen_choice
            save_config(self.cfg)
        self.engine = PlaybackEngine(demo=demo)
        app.aboutToQuit.connect(self.engine.stop)
        self.engine.set_offset(float(self.cfg.get("offset", 0.0)))
        if self.cfg.get("last_player"):
            self.engine.set_player(self.cfg["last_player"])

        self.full = FullAppWindow(self)
        self.overlay = OverlayWindow(self)
        self.tray = None
        self._build_tray()

        # Engine -> tray tooltip.
        self.engine.position_changed.connect(self._update_tray_tooltip)
        self.engine.track_changed.connect(lambda _t: self._update_tray_tooltip(0, -1))

        if mode == "overlay":
            self.show_overlay()
        else:
            self.show_full()

    # ------------------------------------------------------------------ tray
    def _build_tray(self):
        if self.tray is not None:
            self.tray.hide()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return  # headless / no tray: app still works, tray is optional
        self.tray = QSystemTrayIcon(make_tray_icon(), self.app)
        menu = QMenu()
        a = QAction("Show Full App", self.app)
        a.triggered.connect(self.show_full)
        menu.addAction(a)
        a = QAction("Show Overlay", self.app)
        a.triggered.connect(self.show_overlay)
        menu.addAction(a)
        menu.addSeparator()
        a = QAction("Play/Pause", self.app)
        a.triggered.connect(self.engine.play_pause)
        menu.addAction(a)
        a = QAction("Next", self.app)
        a.triggered.connect(self.engine.next_track)
        menu.addAction(a)
        a = QAction("Previous", self.app)
        a.triggered.connect(self.engine.previous_track)
        menu.addAction(a)
        menu.addSeparator()
        menu.addMenu(build_display_menu(
            self.app, self.app, self.cfg.get("display", "primary"),
            self.pick_display))
        self.min_tray_act = QAction("Minimise to tray on close", self.app, checkable=True)
        self.min_tray_act.setChecked(bool(self.cfg.get("minimise_to_tray", True)))
        self.min_tray_act.triggered.connect(self.toggle_minimise_to_tray)
        menu.addAction(self.min_tray_act)
        a = QAction("Quit", self.app)
        a.triggered.connect(self.quit_app)
        menu.addAction(a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            if self.full.isVisible():
                self.full.hide()
            else:
                self.show_full()

    def _update_tray_tooltip(self, pos, idx):
        if not self.tray:
            return
        track = self.engine.track
        line = self.engine.current_line_text
        tip = track.display
        if line:
            tip += f"\n{line}"
        self.tray.setToolTip(tip[:200])

    # ----------------------------------------------------------- mode switching
    def show_full(self):
        self.overlay.hide()
        self.full.show()
        self.full.raise_()
        self.full.activateWindow()

    def show_overlay(self):
        self.full.hide()
        move_window_to_screen(self.overlay, self.app, self.cfg.get("display", "primary"))
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.activateWindow()

    def pick_display(self, choice):
        self.cfg["display"] = choice
        save_config(self.cfg)
        # The overlay follows the choice immediately; the Full App only
        # moves on the explicit menu action (so it doesn't jump mid-use).
        if self.overlay.isVisible():
            move_window_to_screen(self.overlay, self.app, choice)
        # Rebuild menus so the checkmark moves: simplest is to rebuild tray.
        if self.tray:
            self._build_tray()

    def set_overlay_opacity(self, value: float):
        self.cfg["overlay_opacity"] = max(0.3, min(1.0, value))
        save_config(self.cfg)
        self.overlay._apply_opacity()

    def set_overlay_click_through(self, on: bool):
        self.cfg["overlay_click_through"] = bool(on)
        save_config(self.cfg)
        self.overlay._apply_click_through()

    def toggle_minimise_to_tray(self, checked: bool):
        self.cfg["minimise_to_tray"] = bool(checked)
        save_config(self.cfg)

    def quit_app(self):
        self.engine.stop()
        self.app.quit()


# ---------------------------------------------------------------------- CLI
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="synced-lyrics-linux",
        description="Native Linux synced lyrics (MPRIS + LRCLIB).")
    p.add_argument("--mode", choices=["full", "overlay"], default="full",
                   help="start in Full App or Overlay mode (default: full)")
    p.add_argument("--screen", type=int, default=None, metavar="N",
                   help="launch on display N (0-based, see View > Display)")
    p.add_argument("--demo", action="store_true",
                   help="demo mode: fake Coldplay — Yellow with sample lyrics, no player needed")
    p.add_argument("--test-lyrics", nargs=2, metavar=("ARTIST", "TITLE"),
                   help="fetch lyrics for ARTIST TITLE from LRCLIB and print them, then exit")
    return p.parse_args(argv)


def test_lyrics_cli(artist: str, title: str) -> int:
    try:
        result = fetch_lrclib(artist, title)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if result is None:
        print(f"No lyrics found on LRCLIB for {artist} — {title}")
        return 1
    print(f"{result.artist} — {result.track}  (source: {result.source}"
          f"{', instrumental' if result.instrumental else ''})")
    if result.has_synced:
        for line in result.synced_lines:
            print(f"[{int(line.time // 60):02d}:{line.time % 60:05.2f}] {line.text}")
    elif result.plain_lyrics:
        print(result.plain_lyrics)
    else:
        print("(no lyrics text)")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.test_lyrics:
        return test_lyrics_cli(args.test_lyrics[0], args.test_lyrics[1])

    app = QApplication(sys.argv if argv is None else [sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_tray_icon())
    controller = AppController(app, demo=args.demo, mode=args.mode,
                               screen_choice=args.screen)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
