#!/usr/bin/env python3
"""synced-lyrics-linux — a native Linux synced-lyrics app.

Repo: https://github.com/konn-neko/synced-lyrics-linux
A BetterLyrics-style app for Linux: reads what's playing via MPRIS
(Spotify, YouTube Music in a browser, VLC, ...), fetches word-synced
lyrics from the Better Lyrics API (TTML, cache-first, no key) with
LRCLIB line sync as fallback, and shows them in sync.

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
import html
import json
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QLockFile, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
    QUrl,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage,
    QLinearGradient, QPainter, QPixmap, QTextCharFormat, QTextCursor,
    QTextDocument, QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog,
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QSizeGrip,
    QSlider, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget, QWidgetAction,
)

import mpris
from lyrics import fetch_lrclib, log_request, request_log_text
from playback import PlaybackEngine

APP_NAME = "synced-lyrics-linux"
REPO_URL = "https://github.com/konn-neko/synced-lyrics-linux"
CONFIG_DIR = Path.home() / ".config" / "synced-lyrics-linux"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOCK_FILE = CONFIG_DIR / "synced-lyrics.lock"
SERVER_NAME = "synced-lyrics-linux"  # QLocalServer name for raise-existing
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
    "line_animations": True,  # smooth scroll + crossfade; False = instant swap
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
    """A QMenu listing every display + Primary / Follow mouse.

    parent must be a QWidget (or None) — QMenu rejects a QApplication
    as parent, which crashed startup on real desktops where the tray
    menu is built (headless/offscreen never reached it, because the
    tray is unavailable there and _build_tray returns early).
    """
    menu_parent = parent if isinstance(parent, QWidget) else None
    menu = QMenu("Display", menu_parent)
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


# ------------------------------------------------------- line-change animation
# Line changes used to be instant: the highlight colour/font swapped and
# scrollToItem jumped in one frame, and the overlay setText() swapped
# with no transition at all. That reads as "snappy". These helpers make
# the change glide instead. One animation object each, reused for the
# life of the window; no polling, no extra threads.
# Durations: Full App scroll 350ms, colour crossfade ~150ms,
# Overlay fade-out 150ms + fade-in 250ms, all QEasingCurve.OutCubic.

def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linear interpolation between two QColors (t=0 -> c1, t=1 -> c2)."""
    return QColor(
        round(c1.red() + (c2.red() - c1.red()) * t),
        round(c1.green() + (c2.green() - c1.green()) * t),
        round(c1.blue() + (c2.blue() - c1.blue()) * t))


def _word_span(text: str, color: str, sung: bool, is_background: bool) -> str:
    """One HTML span for a piece of text (a word or part of one)."""
    if not text:
        return ""
    style = f"color:{color};"
    style += "font-weight:bold;" if sung else "font-weight:normal;"
    if is_background:
        style += "font-style:italic;"
    return f'<span style="{style}">{html.escape(text)}</span>'


def _lerp_hex(c1: str, c2: str, t: float) -> str:
    """Interpolate two #RRGGBB colours; pure (no Qt) for tests.

    Used by the word-transition wash: a character mid-sweep gets a
    colour between the unsung and sung colours instead of flipping.
    Falls back to the nearer endpoint for unparsable input.
    """
    try:
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    except (ValueError, IndexError):
        return c2 if t >= 0.5 else c1
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _render_word_sweep(word, pos: float | None, sung_color: str,
                       unsung_color: str) -> str:
    """Render one word with a mid-word sweep at `pos` (offset applied).

    Completed syllable parts are fully sung; the part containing `pos`
    gets a smooth transition: the sweep front moves continuously
    through it, and each character's colour interpolates between the
    unsung and sung colours as the front passes — a gradient wash
    across the word, instead of whole characters snapping from dim
    to crimson. Parts without timings sweep across the whole word.
    `pos=None` renders the whole word sung (the old behaviour).
    """
    parts = word.parts or [word]
    out = []
    for part in parts:
        if pos is None or pos >= part.end:
            out.append(_word_span(part.text, sung_color, True,
                                  word.is_background))
        elif pos <= part.time:
            out.append(_word_span(part.text, unsung_color, False,
                                  word.is_background))
        else:
            dur = max(0.001, part.end - part.time)
            frac = (pos - part.time) / dur
            n = len(part.text)
            for j, ch in enumerate(part.text):
                # How sung is this character: 1 once the front has
                # fully passed it, 0 before the front reaches it, and
                # a fraction while the front is inside it.
                amount = max(0.0, min(1.0, frac * n - j))
                if amount >= 1.0:
                    out.append(_word_span(ch, sung_color, True,
                                          word.is_background))
                elif amount <= 0.0:
                    out.append(_word_span(ch, unsung_color, False,
                                          word.is_background))
                else:
                    color = _lerp_hex(unsung_color, sung_color, amount)
                    out.append(_word_span(ch, color, False,
                                          word.is_background))
    return "".join(out)


def render_line_html(line, word_idx: int, pos: float | None = None,
                     offset: float = 0.0,
                     sung_color: str = CRIMSON,
                     unsung_color: str = DIM) -> str:
    """HTML for one lyric line with words up to `word_idx` sung.

    Pure helper (no Qt widgets) so tests can cover it without windows.
    Words before `word_idx` are fully sung; the active word sweeps
    mid-word when `pos` is given (see _render_word_sweep — the
    character the sweep front is inside gets an intermediate colour,
    so the highlight transitions smoothly across the word); the rest
    are unsung. Background-vocal words render italic. Lines without
    word timings return their plain text, HTML-escaped — exactly
    today's line-only rendering. With `pos=None` the active word is
    fully sung, exactly the old behaviour. QLabel renders this rich
    text natively; QListWidgetItem does NOT (its default delegate
    shows the tags literally — seen on William's box), so the Full App
    list paints it with LyricsItemDelegate below.
    """
    text = html.escape(line.text)
    if not line.words:
        return text
    eff_pos = None if pos is None else pos + offset
    parts = []
    for i, word in enumerate(line.words):
        if i < word_idx:
            parts.append(_word_span(word.text, sung_color, True,
                                    word.is_background))
        elif i == word_idx:
            parts.append(_render_word_sweep(word, eff_pos, sung_color,
                                            unsung_color))
        else:
            parts.append(_word_span(word.text, unsung_color, False,
                                    word.is_background))
    return " ".join(parts)


def _sweep_front(line, word_idx: int, pos: float | None):
    """Float character position of the sweep front in line.text.

    Pure helper (no Qt) so tests can cover it. Words before word_idx
    are fully sung; inside the active word the front moves through
    its syllable parts at `pos` (the caller applies the sync offset
    first). Returns None when nothing is sung yet, or the line has
    no word timings.
    """
    if word_idx < 0 or not line.words:
        return None
    if word_idx >= len(line.words):
        return None
    offsets = []
    o = 0
    for w in line.words:
        offsets.append(o)
        o += len(w.text) + 1
    word = line.words[word_idx]
    start = offsets[word_idx]
    if pos is None:
        return float(start + len(word.text))
    parts = word.parts or [word]
    sung = 0
    for part in parts:
        if pos >= part.end:
            sung += len(part.text)
        elif pos <= part.time:
            break
        else:
            dur = max(0.001, part.end - part.time)
            return start + sung + (pos - part.time) / dur * len(part.text)
    return float(start + sung)


def _paint_sweep_text(painter, rect, font, text, front, sung_color,
                      unsung_color, alignment=Qt.AlignLeft):
    """Paint text with a pixel-smooth sweep front + glow.

    `front` is the float character position in `text` (_sweep_front).
    Text behind the front is `sung_color` through a soft-edged alpha
    mask with a low-opacity offset glow underneath; text ahead of the
    front is `unsung_color`. HTML alone can only flip whole
    characters, so on a long held syllable the front would visibly
    step — here it is a continuous pixel position, re-painted ~16x/s
    from the interpolated playback position. Shared by the Full App
    delegate (current row) and the overlay label.
    """
    width = max(1, int(rect.width()))

    def make_doc(color):
        doc = QTextDocument()
        doc.setDocumentMargin(2)
        doc.setDefaultFont(font)
        text_option = QTextOption()
        text_option.setAlignment(alignment)
        doc.setDefaultTextOption(text_option)
        doc.setPlainText(text)
        doc.setTextWidth(width)
        # NOTE: a `body { color: ... }` default stylesheet does NOT
        # colour the text in Qt (it renders black) — set the colour
        # via a char format on the whole document instead.
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.mergeCharFormat(fmt)
        return doc

    dim_doc = make_doc(unsung_color)
    doc_h = dim_doc.size().height()
    yoff = max(0.0, (rect.height() - doc_h) / 2)
    painter.save()
    painter.translate(rect.topLeft())
    painter.translate(0, yoff)
    dim_doc.drawContents(painter)
    painter.restore()
    if front is None or front <= 0:
        return
    crimson_doc = make_doc(sung_color)
    img_h = max(1, int(crimson_doc.size().height()) + 4)
    img = QImage(width, img_h, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    crimson_doc.drawContents(p)
    p.end()

    # Alpha mask: opaque where the text is fully sung, a soft
    # gradient across the front, transparent past it.
    mask = QImage(width, img_h, QImage.Format_ARGB32_Premultiplied)
    mask.fill(QColor(0, 0, 0, 0))
    mp = QPainter(mask)
    margin = crimson_doc.documentMargin()
    fm = QFontMetrics(font)
    front_ci = min(len(text), int(front))
    front_frac = front - front_ci
    layout = crimson_doc.firstBlock().layout()
    soft = 8
    for i in range(layout.lineCount()):
        ln = layout.lineAt(i)
        r = QRectF(margin + ln.x(), margin + ln.y(), ln.width(), ln.height())
        line_end = ln.textStart() + ln.textLength()
        if front_ci >= line_end and i < layout.lineCount() - 1:
            # Whole visual line is sung (front is past its end).
            mp.fillRect(r, QColor(255, 255, 255, 255))
        elif ln.textStart() <= front_ci:
            # The front is on (or past the end of) this line.
            x_in = fm.horizontalAdvance(text[ln.textStart():front_ci])
            if front_ci < len(text):
                x_in += front_frac * fm.horizontalAdvance(text[front_ci])
            front_x = margin + ln.x() + x_in
            grad = QLinearGradient(front_x - soft, 0, front_x + soft, 0)
            grad.setColorAt(0.0, QColor(255, 255, 255, 255))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            mp.fillRect(r, grad)
        # Lines after the front stay transparent.
    mp.end()
    mp = QPainter(img)
    mp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    mp.drawImage(0, 0, mask)
    mp.end()

    # Glow: low-opacity offset copies, then the sharp pass.
    painter.save()
    painter.translate(rect.topLeft())
    painter.translate(0, yoff)
    painter.setOpacity(0.30)
    for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2)):
        painter.drawImage(dx, dy, img)
    painter.setOpacity(1.0)
    painter.drawImage(0, 0, img)
    painter.restore()


class LyricsItemDelegate(QStyledItemDelegate):
    """Paint list items as rich text with a QTextDocument.

    QListWidget's default delegate draws item text as plain text, so
    the word-by-word HTML from render_line_html() showed up literally
    on screen ("<span style=...>Ooh,</span>"). This delegate paints
    the HTML instead, sizes each row from the document so long lines
    wrap at the viewport width (no horizontal scrollbar), and keeps
    the default selection/focus background. Plain lines (no HTML) get
    the item's foreground colour as their base colour, so the
    highlight crossfade (which only changes the foreground) still
    works. Row heights are re-queried whenever an item's text changes
    (every word tick / line change), so resizing the window reflows
    rows as lyrics update.

    The current line is painted differently: HTML can only flip
    whole characters, so on a long held syllable the front visibly
    steps. When `sweep_provider` returns data for a row (set by
    FullAppWindow), that row is painted pixel-smooth instead — the
    sung text in crimson through a soft-edged alpha mask with a glow
    underneath (see _paint_sweep).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Optional provider: fn(row) -> {"line", "word_idx", "pos"}
        # for the current row, or None. When it returns data, that
        # row gets the smooth sweep paint instead of the HTML paint.
        self.sweep_provider = None

    def _document(self, option, index):
        doc = QTextDocument()
        doc.setDocumentMargin(2)
        doc.setDefaultFont(option.font)
        fg = option.palette.text().color()
        text = index.data(Qt.DisplayRole) or ""
        doc.setHtml(text)
        if "<" not in text:
            # Plain line (no spans carry a colour): a `body` colour
            # stylesheet does NOT apply in Qt — the line would render
            # black on the dark background. Set it via char format.
            cursor = QTextCursor(doc)
            cursor.select(QTextCursor.Document)
            fmt = QTextCharFormat()
            fmt.setForeground(fg)
            cursor.mergeCharFormat(fmt)
        return doc

    def _plain_document(self, option, text, color):
        """Plain-text document in the item's font, one colour.

        (Layout only — sizeHint. The colour is set via char format:
        a `body` stylesheet colour does not apply in Qt.)
        """
        doc = QTextDocument()
        doc.setDocumentMargin(2)
        doc.setDefaultFont(option.font)
        doc.setPlainText(text)
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.mergeCharFormat(fmt)
        return doc

    def _sweep_for(self, index):
        if self.sweep_provider is None:
            return None
        try:
            return self.sweep_provider(index.row())
        except Exception:
            return None

    def paint(self, painter, option, index):
        sweep = self._sweep_for(index)
        if sweep is not None:
            self._paint_sweep(painter, option, index, sweep)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""  # background only here; the document draws the text
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, opt.widget)
        doc = self._document(opt, index)
        doc.setTextWidth(max(1, opt.rect.width()))
        painter.save()
        painter.translate(opt.rect.topLeft())
        y = (opt.rect.height() - doc.size().height()) / 2
        painter.translate(0, max(0, y))
        doc.drawContents(painter)
        painter.restore()

    def _paint_sweep(self, painter, option, index, sweep):
        """Pixel-smooth sweep with glow for the current line.

        The sung colour is the item's foreground, so the line-change
        colour crossfade still works.
        """
        line = sweep["line"]
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""  # background only here; the sweep paint draws text
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, opt.widget)
        front = _sweep_front(line, sweep["word_idx"], sweep["pos"])
        _paint_sweep_text(painter, opt.rect, opt.font, line.text, front,
                          opt.palette.text().color().name(), DIM)

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        width = 0
        widget = self.parent()
        if widget is not None:
            width = widget.viewport().width()
        if not width:
            width = opt.rect.width() or 400
        sweep = self._sweep_for(index)
        if sweep is not None:
            # Same plain-text document the sweep paint uses, so the
            # row height matches the painted text exactly.
            doc = self._plain_document(
                opt, sweep["line"].text, opt.palette.text().color().name())
            doc.setTextWidth(width)
            return QSize(int(doc.idealWidth()), int(doc.size().height()) + 6)
        doc = self._document(opt, index)
        doc.setTextWidth(width)
        return QSize(int(doc.idealWidth()), int(doc.size().height()) + 6)


class GlowWordLabel(QLabel):
    """QLabel that paints its rich text with a crimson glow behind
    the sung spans.

    QLabel has no text-shadow, and a QGraphicsDropShadowEffect can't
    coexist with the LabelFader's opacity effect on the same label —
    so this paints the glow itself. The label text is rendered a
    second time with every non-crimson colour replaced by the
    background colour (so only the sung spans show), drawn at low
    opacity with small offsets, then the normal text is drawn on top.
    """

    # Colours that mean "not sung" in the overlay's word HTML.
    UNSUNG_COLORS = ("#c9d1d9", "#8b949e")
    GLOW_BG = "#0d1117"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional provider: fn() -> {"line", "word_idx", "pos"} for
        # the current line, or None. Set by OverlayWindow; when it
        # returns data the label paints the smooth sweep (same as the
        # Full App delegate) instead of its HTML text.
        self.sweep_provider = None

    def paintEvent(self, event):
        sweep = None
        if self.sweep_provider is not None:
            try:
                sweep = self.sweep_provider()
            except Exception:
                sweep = None
        if sweep is not None:
            front = _sweep_front(sweep["line"], sweep["word_idx"], sweep["pos"])
            p = QPainter(self)
            _paint_sweep_text(p, self.rect(), self.font(),
                              sweep["line"].text, front, CRIMSON, "#c9d1d9",
                              alignment=self.alignment())
            p.end()
            return
        text = self.text()
        if not text:
            return
        width = max(1, self.width())
        text_option = QTextOption()
        text_option.setAlignment(self.alignment())

        def make_doc(html_text):
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(self.font())
            doc.setDefaultTextOption(text_option)
            doc.setHtml(html_text)
            doc.setTextWidth(width)
            return doc

        doc = make_doc(text)
        yoff = max(0.0, (self.height() - doc.size().height()) / 2)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Glow pass: only the sung (crimson) spans, offset + faint.
        glow_html = text
        for c in self.UNSUNG_COLORS:
            glow_html = glow_html.replace(c, self.GLOW_BG)
        if glow_html != text:
            gdoc = make_doc(glow_html)
            p.save()
            p.translate(0, yoff)
            p.setOpacity(0.35)
            for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2)):
                p.save()
                p.translate(dx, dy)
                gdoc.drawContents(p)
                p.restore()
            p.restore()
        p.save()
        p.translate(0, yoff)
        doc.drawContents(p)
        p.restore()


class SmoothScroller:
    """Animate a QScrollBar's value to a target instead of jumping.

    Retargeting mid-animation is safe: stop() the running animation and
    start a new one from the scrollbar's current value, so a fast run
    of line changes never queues up or overshoots.
    """

    DURATION_MS = 350

    def __init__(self, scrollbar):
        self.bar = scrollbar
        self.anim = QPropertyAnimation(scrollbar, b"value", scrollbar)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.setDuration(self.DURATION_MS)

    def scroll_to(self, target, enabled=True):
        target = max(self.bar.minimum(), min(self.bar.maximum(), int(target)))
        if not enabled:
            self.anim.stop()
            self.bar.setValue(target)
            return
        if self.bar.value() == target:
            return
        self.anim.stop()
        self.anim.setStartValue(self.bar.value())
        self.anim.setEndValue(target)
        self.anim.start()

    def stop(self):
        self.anim.stop()


class LabelFader:
    """Crossfade a QLabel's text: fade out, swap text, fade in.

    One QGraphicsOpacityEffect + one QPropertyAnimation on its opacity,
    reused. If show_text() is called mid-fade, the pending text is
    updated (mid-fade-out) or the fade restarts (mid-fade-in), so the
    label always lands on the newest text — never a stale line, never
    two animations fighting. No geometry animation: the label stays
    layout-managed, only its opacity moves.
    """

    FADE_OUT_MS = 150
    FADE_IN_MS = 250

    def __init__(self, label: QLabel):
        self.label = label
        self.effect = QGraphicsOpacityEffect(label)
        self.effect.setOpacity(1.0)
        label.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity", label)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.finished.connect(self._on_finished)
        self._state = "idle"  # idle | out | in
        self._pending = None
        self._ignore_finished = False

    @property
    def busy(self) -> bool:
        """True while a crossfade is in flight. Word-only updates
        (same line, new word) must wait for idle and set the label
        text directly, or the fader would land on stale text."""
        return self._state != "idle"

    def show_text(self, text, enabled=True):
        if not enabled:
            self._cancel()
            self.label.setText(text)
            self.effect.setOpacity(1.0)
            return
        if text == self.label.text() and self._state == "idle":
            if self.effect.opacity() < 1.0:
                self.effect.setOpacity(1.0)
            return
        if self._state == "out":
            # Already fading out: just swap what we land on.
            self._pending = text
            return
        self._pending = text
        self._start_fade_out()

    def _start_fade_out(self):
        self._state = "out"
        self._ignore_finished = True  # stop() must not trigger _on_finished
        self.anim.stop()
        self._ignore_finished = False
        self.anim.setDuration(self.FADE_OUT_MS)
        self.anim.setStartValue(self.effect.opacity())
        self.anim.setEndValue(0.0)
        self.anim.start()

    def _start_fade_in(self):
        self._state = "in"
        self._ignore_finished = True
        self.anim.stop()
        self._ignore_finished = False
        self.anim.setDuration(self.FADE_IN_MS)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()

    def _cancel(self):
        self._ignore_finished = True
        try:
            self.anim.stop()
        finally:
            self._ignore_finished = False
        self._state = "idle"
        self._pending = None

    def _on_finished(self):
        if self._ignore_finished:
            return
        if self._state == "out":
            if self._pending is not None:
                self.label.setText(self._pending)
                self._pending = None
            self._start_fade_in()
        elif self._state == "in":
            self._state = "idle"
            self.effect.setOpacity(1.0)


# ------------------------------------------------------------- shared view
class LyricsViewMixin:
    """Shared helpers for the two windows: both render from the engine."""

    def _apply_lyrics(self, lyrics):
        raise NotImplementedError


# --------------------------------------------------------------- full app
def build_lyrics_debug_text(engine) -> str:
    """Plain-text debug dump for Help > Lyrics debug.

    Current track, lyrics source, the raw lyrics file exactly as
    received, and every HTTP request this run with its response.
    Pure function (takes the engine) so tests can cover it without
    opening the dialog.
    """
    out = []
    track = engine.track
    out.append(f"Track: {track.display or '(nothing playing)'}")
    out.append(f"Player: {track.player or '-'}   Status: {track.status or '-'}")
    lyrics = engine.lyrics
    if lyrics is None:
        out.append("Lyrics: none loaded")
    else:
        n_words = sum(len(l.words) for l in lyrics.synced_lines)
        out.append(f"Lyrics source: {lyrics.source}")
        out.append(f"Lines: {len(lyrics.synced_lines)}   Words: {n_words}"
                   f"   Word sync: {lyrics.has_word_sync}")
        if lyrics.cache_file:
            out.append(f"Cache file: {lyrics.cache_file}")
        out.append("")
        out.append(f"=== Raw lyrics file ({lyrics.raw_kind or 'unknown'}) ===")
        out.append(lyrics.raw_text or "(no raw text stored for this result)")
    out.append("")
    out.append("=== HTTP requests (this run) ===")
    out.append(request_log_text())
    return "\n".join(out)


class LyricsDebugDialog(QDialog):
    """Help > Lyrics debug: raw lyrics file + all URL queries/responses."""

    def __init__(self, parent, engine):
        super().__init__(parent)
        self.setWindowTitle("Lyrics debug")
        self.resize(800, 600)
        layout = QVBoxLayout(self)
        text = QTextEdit(self)
        text.setReadOnly(True)
        text.setFont(QFont("monospace"))
        text.setPlainText(build_lyrics_debug_text(engine))
        layout.addWidget(text)
        row = QHBoxLayout()
        copy_btn = QPushButton("Copy all")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(text.toPlainText()))
        row.addWidget(copy_btn)
        row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)


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
        self._current_word_idx = -2
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
        # Rich-text rows: the default delegate shows render_line_html()
        # tags literally; LyricsItemDelegate paints the HTML instead.
        self.lyrics_list.setItemDelegate(LyricsItemDelegate(self.lyrics_list))
        # The delegate paints the current line pixel-smooth (soft edge
        # + glow) from this provider instead of the per-character HTML.
        self.lyrics_list.itemDelegate().sweep_provider = self._sweep_for_row
        # ScrollPerPixel is REQUIRED for the smooth scroll: the default
        # ScrollPerItem measures the scrollbar in whole items, so the
        # pixel target SmoothScroller computes was clamped to the item
        # count and the list lurched between the extremes instead of
        # gliding — the up/down scroll bug. With per-pixel scrolling
        # the scrollbar value really is pixels and the target math holds.
        self.lyrics_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.lyrics_list.setWordWrap(True)
        self.lyrics_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lyrics_list.setStyleSheet(
            f"background:{BG}; border:1px solid {PANEL}; border-radius:6px;")
        self.lyrics_list.itemClicked.connect(self._on_line_clicked)
        layout.addWidget(self.lyrics_list, stretch=1)

        # Line-change animation state: one reused scroller for the glide
        # to the current line, one QTimer stepping the highlight colour
        # crossfade. _fade_prev/_fade_cur are the items being faded.
        self._scroller = SmoothScroller(self.lyrics_list.verticalScrollBar())
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(30)
        self._fade_timer.timeout.connect(self._on_fade_tick)
        self._fade_prev = None
        self._fade_cur = None
        self._fade_step = 0
        self._fade_steps = 5  # 5 x 30ms = ~150ms colour crossfade

        # Mid-word sweep: re-render the current line's word fill ~16x/s
        # from the engine's interpolated position. This is a UI repaint
        # timer only — it never touches MPRIS (no second poller), it
        # just reads the monotonic estimate between 250 ms polls.
        self._fill_timer = QTimer(self)
        self._fill_timer.setInterval(60)
        self._fill_timer.timeout.connect(self._on_fill_tick)
        self._fill_timer.start()

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
        self.anim_act = QAction("Smooth line animations", self, checkable=True)
        self.anim_act.setChecked(bool(self.cfg.get("line_animations", True)))
        self.anim_act.triggered.connect(
            lambda checked: c.set_line_animations(checked))
        view_menu.addAction(self.anim_act)

        help_menu = menubar.addMenu("&Help")
        a = QAction("Lyrics debug", self)
        a.setToolTip("Show the raw lyrics file and every URL query + response")
        a.triggered.connect(c.show_lyrics_debug)
        help_menu.addAction(a)
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
                    data = r.read()
                    log_request(url, f"HTTP {r.status}",
                                f"<{len(data)} bytes of image data>")
                    path.write_bytes(data)
            from PySide6.QtGui import QPixmap as _PM
            pm = _PM(str(path))
            if not pm.isNull():
                self.art_label.setPixmap(pm.scaled(64, 64, Qt.KeepAspectRatio,
                                                   Qt.SmoothTransformation))
                self.art_label.setText("")
        except Exception:
            pass  # art is decorative; never break playback for it

    def _on_lyrics(self, lyrics):
        # Stop any in-flight line animation BEFORE clearing: the fade
        # holds QListWidgetItems that clear() would delete.
        self._scroller.stop()
        self._fade_timer.stop()
        self._fade_prev = None
        self._fade_cur = None
        self._fade_step = 0
        self.lyrics_list.clear()
        self._current_idx = -2
        self._current_word_idx = -2
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
        if not self.engine.lyrics or not self.engine.lyrics.has_synced:
            return
        if idx != self._current_idx:
            self._highlight(idx, pos)
            return
        # Same line: only the word may have changed. Update the word
        # highlight in place — this must NOT call _highlight(), or the
        # scroll glide would restart on every word.
        line_idx, word_idx = self.engine.word_index_at(pos)
        if word_idx != self._current_word_idx and line_idx == idx:
            self._current_word_idx = word_idx
            self._render_current_words()

    def _highlight(self, idx, pos=None):
        # Land any in-flight colour fade on its final colours first, so
        # a fast run of line changes never leaves a stale colour behind.
        self._finish_fade()
        animated = bool(self.cfg.get("line_animations", True))
        prev = self._current_idx
        prev_item = None
        if 0 <= prev < self.lyrics_list.count():
            prev_item = self.lyrics_list.item(prev)
            f = QFont(self.font())
            f.setPointSize(self.cfg["font_size"])
            f.setBold(False)
            prev_item.setFont(f)
            # Restore the plain line text: it may carry word HTML
            # from when it was the current line.
            lyrics = self.engine.lyrics
            if lyrics and 0 <= prev < len(lyrics.synced_lines):
                prev_item.setText(lyrics.synced_lines[prev].text)
        self._current_idx = idx
        self._current_word_idx = -2
        if pos is not None and self.engine.lyrics:
            line_idx, widx = self.engine.word_index_at(pos)
            if line_idx == idx:
                self._current_word_idx = widx
        if not (0 <= idx < self.lyrics_list.count()):
            if prev_item is not None:
                prev_item.setForeground(QColor(DIM))
            return
        item = self.lyrics_list.item(idx)
        f = QFont(self.font())
        f.setPointSize(self.cfg["font_size"] + 2)
        f.setBold(True)
        item.setFont(f)
        # Colours: instant final values when animations are off; when on,
        # start from the old colours and let _on_fade_tick walk them to
        # the final ones over ~150ms (prev crimson -> dim, cur dim -> crimson).
        if animated:
            if prev_item is not None:
                prev_item.setForeground(QColor(CRIMSON))
            item.setForeground(QColor(DIM))
            self._fade_prev = prev_item
            self._fade_cur = item
            self._fade_step = 0
            self._fade_timer.start()
        else:
            if prev_item is not None:
                prev_item.setForeground(QColor(DIM))
            item.setForeground(QColor(CRIMSON))
        # Word-by-word: render the current line's words (sung words
        # crimson, rest dim). Plain text for lines without timings.
        self._render_current_words()
        # Smooth-scroll: glide the new line to the centre (~350ms,
        # OutCubic) instead of scrollToItem's instant jump. The target
        # is computed from the item rect AFTER the font change, so row
        # heights are final. Retargeting mid-glide restarts from the
        # current scroll value (see SmoothScroller).
        bar = self.lyrics_list.verticalScrollBar()
        rect = self.lyrics_list.visualItemRect(item)
        target = (rect.top() + bar.value() + rect.height() // 2
                  - self.lyrics_list.viewport().height() // 2)
        self._scroller.scroll_to(target, enabled=animated)

    def _sweep_for_row(self, row):
        """Smooth-glow paint data for the current lyrics row, or None.

        The delegate paints the current line pixel-smooth (soft edge
        + glow) from this instead of the per-character HTML: the HTML
        can only flip whole characters, so on a long held syllable
        the front visibly steps. The provider reads the live
        interpolated position, so the front moves continuously.
        """
        lyrics = self.engine.lyrics
        if not lyrics or not lyrics.has_word_sync:
            return None
        if row != self._current_idx:
            return None
        if not (0 <= row < len(lyrics.synced_lines)):
            return None
        return {"line": lyrics.synced_lines[row],
                "word_idx": self._current_word_idx,
                "pos": self.engine.current_position() + self.engine.offset}

    def _render_current_words(self):
        """Apply word-by-word HTML to the current line's list item.

        Lines without word timings keep their plain text, so line-only
        tracks render exactly as before. The active word is rendered
        with the mid-word sweep at the current interpolated position.
        """
        idx = self._current_idx
        if not (0 <= idx < self.lyrics_list.count()):
            return
        lyrics = self.engine.lyrics
        if not lyrics or idx >= len(lyrics.synced_lines):
            return
        item = self.lyrics_list.item(idx)
        item.setText(render_line_html(
            lyrics.synced_lines[idx], self._current_word_idx,
            self.engine.current_position(), offset=self.engine.offset))

    def _on_fill_tick(self):
        """Mid-word sweep: re-render the current line's word fill from
        the interpolated position, ~16x/s. Only the current line's
        item text changes, and only when the HTML actually changed,
        so this is cheap; line changes still go through _highlight().
        """
        lyrics = self.engine.lyrics
        if not lyrics or not lyrics.has_word_sync:
            return
        if self.engine.track.status != "Playing":
            return
        idx = self._current_idx
        if not (0 <= idx < self.lyrics_list.count()):
            return
        pos = self.engine.current_position()
        line_idx, word_idx = self.engine.word_index_at(pos)
        if line_idx != idx:
            return  # line change: _on_position / _highlight owns it
        if word_idx != self._current_word_idx:
            self._current_word_idx = word_idx
        item = self.lyrics_list.item(idx)
        html = render_line_html(lyrics.synced_lines[idx], word_idx, pos,
                                offset=self.engine.offset)
        if html != item.text():
            item.setText(html)
        # The smooth sweep paint reads the live position, not the item
        # text — repaint every tick so the front moves continuously
        # even while the HTML is unchanged (long held syllable).
        self.lyrics_list.viewport().update()

    def _on_fade_tick(self):
        self._fade_step += 1
        t = min(1.0, self._fade_step / self._fade_steps)
        if self._fade_cur is not None:
            self._fade_cur.setForeground(
                _lerp_color(QColor(DIM), QColor(CRIMSON), t))
        if self._fade_prev is not None:
            self._fade_prev.setForeground(
                _lerp_color(QColor(CRIMSON), QColor(DIM), t))
        if t >= 1.0:
            self._fade_timer.stop()
            self._fade_cur = None
            self._fade_prev = None

    def _finish_fade(self):
        """Apply the final colours of any in-flight crossfade at once."""
        if self._fade_timer.isActive():
            self._fade_timer.stop()
        if self._fade_cur is not None:
            self._fade_cur.setForeground(QColor(CRIMSON))
            self._fade_cur = None
        if self._fade_prev is not None:
            self._fade_prev.setForeground(QColor(DIM))
            self._fade_prev = None
        self._fade_step = 0

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
        self._current_word_idx = -2
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

        self.current_label = GlowWordLabel("♪")
        self.current_label.setWordWrap(True)
        self.current_label.setAlignment(Qt.AlignCenter)
        self.current_label.setStyleSheet(f"color:{CRIMSON}; font-size:30px; font-weight:bold;")
        # The label paints the current line pixel-smooth (soft edge +
        # glow) from this provider instead of the per-character HTML.
        self.current_label.sweep_provider = self._sweep_for_label
        layout.addWidget(self.current_label, stretch=1)

        self.next_label = QLabel("")
        self.next_label.setWordWrap(True)
        self.next_label.setAlignment(Qt.AlignCenter)
        self.next_label.setStyleSheet(f"color:{OBSIDIAN}; font-size:16px;")
        layout.addWidget(self.next_label)

        # Line-change crossfade: one LabelFader per label, reused.
        # show_text() fades the old line out (~150ms), swaps the text,
        # fades the new line in (~250ms). No geometry animation — the
        # labels stay layout-managed, only their opacity moves.
        self._current_fader = LabelFader(self.current_label)
        self._next_fader = LabelFader(self.next_label)

        # Mid-word sweep: re-render the current line's word fill ~16x/s
        # from the interpolated position (UI repaint timer only — it
        # never touches MPRIS, just reads the engine's estimate).
        self._fill_timer = QTimer(self)
        self._fill_timer.setInterval(60)
        self._fill_timer.timeout.connect(self._on_fill_tick)
        self._fill_timer.start()

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
        anim = QAction("Smooth line animations", self, checkable=True)
        anim.setChecked(bool(self.cfg.get("line_animations", True)))
        anim.triggered.connect(lambda checked: c.set_line_animations(checked))
        menu.addAction(anim)
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
        a = QAction("Lyrics debug", self)
        a.triggered.connect(c.show_lyrics_debug)
        menu.addAction(a)
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
        self._current_word_idx = -2
        animated = bool(self.cfg.get("line_animations", True))
        if lyrics is None or not lyrics.has_synced:
            self._current_fader.show_text(
                "♪" if not lyrics else "No synced lyrics", enabled=animated)
            self._next_fader.show_text("", enabled=animated)
            return
        self._on_position(0.0, -1)

    def _current_line_html(self, idx, pos):
        """Current line rendered with word-by-word highlight at `pos`."""
        lyrics = self.engine.lyrics
        if lyrics is None or not (0 <= idx < len(lyrics.synced_lines)):
            return ""
        _, word_idx = self.engine.word_index_at(pos)
        self._current_word_idx = word_idx
        return render_line_html(lyrics.synced_lines[idx], word_idx, pos,
                                offset=self.engine.offset,
                                sung_color=CRIMSON, unsung_color="#c9d1d9")

    def _sweep_for_label(self):
        """Smooth-sweep paint data for the overlay's current line.

        Only when the line crossfade is idle: mid-fade the label is
        showing the old line fading out, and the sweep must not jump
        it to the new line early.
        """
        fader = getattr(self, "_current_fader", None)
        if fader is not None and fader.busy:
            return None
        lyrics = self.engine.lyrics
        if not lyrics or not lyrics.has_synced or not lyrics.has_word_sync:
            return None
        idx = self._current_idx
        if idx < 0 or idx >= len(lyrics.synced_lines):
            return None
        return {"line": lyrics.synced_lines[idx],
                "word_idx": self._current_word_idx,
                "pos": self.engine.current_position() + self.engine.offset}

    def _on_fill_tick(self):
        """Mid-word sweep: re-render the current line's word fill from
        the interpolated position, ~16x/s. Skipped while a line
        crossfade is in flight (the fader owns the label then) and
        whenever the HTML hasn't actually changed."""
        lyrics = self.engine.lyrics
        if not lyrics or not lyrics.has_synced or not lyrics.has_word_sync:
            return
        if self.engine.track.status != "Playing":
            return
        if self._current_fader.busy:
            return
        idx = self._current_idx
        if idx < 0:
            return
        pos = self.engine.current_position()
        line_idx, word_idx = self.engine.word_index_at(pos)
        if line_idx != idx:
            return
        if word_idx != self._current_word_idx:
            self._current_word_idx = word_idx
        html = render_line_html(lyrics.synced_lines[idx], word_idx, pos,
                                offset=self.engine.offset,
                                sung_color=CRIMSON, unsung_color="#c9d1d9")
        if html != self.current_label.text():
            self.current_label.setText(html)
        # The smooth sweep paint reads the live position, not the
        # label text — repaint every tick so the front moves
        # continuously even while the HTML is unchanged (long held
        # syllable).
        self.current_label.update()

    def _on_position(self, pos, idx):
        lyrics = self.engine.lyrics
        track = self.engine.track
        self.play_btn.setText("⏸" if track.status == "Playing" else "▶")
        if not lyrics or not lyrics.has_synced:
            return
        if idx == self._current_idx:
            # Same line: only the word may have changed. Update the
            # label directly — never re-trigger the line crossfade.
            # If a fade is in flight, skip this tick; the next 250 ms
            # tick retries once the fader is idle.
            _, word_idx = self.engine.word_index_at(pos)
            if (word_idx != self._current_word_idx
                    and not self._current_fader.busy):
                self._current_word_idx = word_idx
                if idx >= 0:
                    self.current_label.setText(
                        render_line_html(lyrics.synced_lines[idx], word_idx, pos,
                                         offset=self.engine.offset,
                                         sung_color=CRIMSON,
                                         unsung_color="#c9d1d9"))
            return
        self._current_idx = idx
        self._current_word_idx = -2
        animated = bool(self.cfg.get("line_animations", True))
        if idx < 0:
            self._current_fader.show_text("♪", enabled=animated)
            self._next_fader.show_text(
                lyrics.synced_lines[0].text if lyrics.synced_lines else "",
                enabled=animated)
        else:
            # Line change: crossfade to the word-highlighted line, so
            # the fade lands on the correct words at once.
            self._current_fader.show_text(
                self._current_line_html(idx, pos), enabled=animated)
            nxt = lyrics.synced_lines[idx + 1].text if idx + 1 < len(lyrics.synced_lines) else ""
            self._next_fader.show_text(nxt, enabled=animated)


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
        app.aboutToQuit.connect(self._hide_all)
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
            menu, self.app, self.cfg.get("display", "primary"),
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

    def set_line_animations(self, enabled: bool):
        """Persist the smooth-line-animations toggle.

        Both windows read cfg["line_animations"] live at each line
        change, so this just saves it and keeps the Full App View menu
        checkmark in sync (the overlay context menu is rebuilt fresh
        on every right-click, so it reads the value itself).
        """
        self.cfg["line_animations"] = bool(enabled)
        save_config(self.cfg)
        act = getattr(self.full, "anim_act", None)
        if act is not None:
            act.setChecked(bool(enabled))

    def show_lyrics_debug(self):
        """Help > Lyrics debug: raw lyrics file + URL queries/responses."""
        dlg = LyricsDebugDialog(self.full, self.engine)
        dlg.exec()

    def quit_app(self):
        # Hide everything first, tray icon included: otherwise KDE
        # keeps a ghost ♪ in the panel after the process is gone.
        self._hide_all()
        self.engine.stop()
        self.app.quit()

    def _hide_all(self):
        """Hide every visible piece (tray icon, windows, handle).

        Also connected to aboutToQuit as belt-and-braces, so a quit
        from any path removes the tray icon instead of leaving a ghost.
        Guards with getattr: aboutToQuit can fire before __init__
        finishes setting these attributes.
        """
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.hide()
        overlay = getattr(self, "overlay", None)
        if overlay is not None:
            overlay.hide()
            handle = getattr(overlay, "handle", None)
            if handle is not None:
                handle.hide()
        full = getattr(self, "full", None)
        if full is not None:
            full.hide()


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


def _raise_existing_window() -> bool:
    """Ask a running instance to show its window. True if one answered.

    A second `synced-lyrics` launch connects to the first instance's
    QLocalServer and exits; the first instance raises its Full App.
    """
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(500):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.test_lyrics:
        return test_lyrics_cli(args.test_lyrics[0], args.test_lyrics[1])

    # Single instance: closing the window only minimises to tray, so
    # without this every fresh launch piled up another live process
    # (and another ♪ in the KDE panel). The lock lives on the app
    # object so it is held for the whole run; QLockFile releases a
    # stale lock if a previous run crashed.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(LOCK_FILE))
    if not lock.tryLock(0):
        if _raise_existing_window():
            print("synced-lyrics is already running — showing the existing window")
        else:
            print("synced-lyrics is already running (could not reach it to raise the window)")
        return 0

    app = QApplication(sys.argv if argv is None else [sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_tray_icon())
    app._single_instance_lock = lock  # keep the lock held for the run
    controller = AppController(app, demo=args.demo, mode=args.mode,
                               screen_choice=args.screen)
    # First instance also listens: a later launch asks us to raise.
    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)  # stale socket file, if any
    if server.listen(SERVER_NAME):
        server.newConnection.connect(
            lambda: (server.nextPendingConnection().deleteLater()
                     if server.hasPendingConnections() else None,
                     controller.show_full()))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
