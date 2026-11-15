"""MPRIS client for synced-lyrics-linux.

Primary: PySide6.QtDBus talking to org.mpris.MediaPlayer2.<player>.
Fallback: `playerctl` CLI (installed on most desktops; `pacman -S playerctl`).
No player names are hardcoded -- players are discovered via ListNames /
`playerctl -l`, so Spotify, YouTube Music in Firefox/Chromium, VLC, Elisa,
etc. all work the same way. No login, no API keys.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class TrackInfo:
    player: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0      # seconds, 0 = unknown
    position: float = 0.0      # seconds, as last reported by the player
    status: str = "Stopped"    # Playing | Paused | Stopped
    art_url: str = ""

    @property
    def key(self) -> tuple:
        return (self.artist.lower().strip(), self.title.lower().strip(),
                self.album.lower().strip(), round(self.duration))

    @property
    def display(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        return self.title or "Nothing playing"


# ---------------------------------------------------------------- QtDBus ----
def _unwrap(value):
    """Best-effort conversion of a QDBusVariant / QVariant to plain Python."""
    if value is None:
        return None
    # QDBusVariant / QVariant
    for attr in ("value", "toPyObject"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)()
                if v is not value:
                    return _unwrap(v)
            except Exception:
                pass
    if isinstance(value, dict):
        return {str(k): _unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unwrap(v) for v in value]
    return value


def _dbus_available() -> bool:
    try:
        from PySide6.QtDBus import QDBusConnection
        return QDBusConnection.sessionBus().isConnected()
    except Exception:
        return False


def _is_error(reply) -> bool:
    """True if a QDBus reply is an error.

    In PySide6 the enum lives at QDBusMessage.MessageType.ErrorMessage —
    `reply.ErrorMessage` does not exist and raises AttributeError, which
    crashed play_pause/next/previous/seek on real desktops (headless
    tests never reached a live D-Bus reply, so they missed it).
    """
    try:
        from PySide6.QtDBus import QDBusMessage
        return reply.type() == QDBusMessage.MessageType.ErrorMessage
    except Exception:
        return False


def _dbus_get(service: str, interface: str, prop: str):
    from PySide6.QtDBus import QDBusConnection, QDBusInterface
    bus = QDBusConnection.sessionBus()
    iface = QDBusInterface(service, "/org/mpris/MediaPlayer2",
                           "org.freedesktop.DBus.Properties", bus)
    if not iface.isValid():
        return None
    reply = iface.call("Get", interface, prop)
    if _is_error(reply):
        return None
    args = reply.arguments()
    if not args:
        return None
    return _unwrap(args[0])


def _dbus_call(service: str, interface: str, method: str, *args):
    from PySide6.QtDBus import QDBusConnection, QDBusInterface
    bus = QDBusConnection.sessionBus()
    iface = QDBusInterface(service, "/org/mpris/MediaPlayer2", interface, bus)
    if not iface.isValid():
        return False
    reply = iface.call(method, *args)
    return not _is_error(reply)


def list_players_dbus() -> list[str]:
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return []
        iface = QDBusInterface("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus", bus)
        reply = iface.call("ListNames")
        if _is_error(reply):
            return []
        names = _unwrap(reply.arguments()[0]) if reply.arguments() else []
        prefix = "org.mpris.MediaPlayer2."
        return sorted(n[len(prefix):] for n in names
                      if isinstance(n, str) and n.startswith(prefix)
                      and not n.startswith(prefix + "Player"))
    except Exception:
        return []


def get_track_dbus(player: str) -> TrackInfo | None:
    service = f"org.mpris.MediaPlayer2.{player}"
    try:
        meta = _dbus_get(service, "org.mpris.MediaPlayer2.Player", "Metadata")
        if not isinstance(meta, dict) or not meta:
            return None
        artist = meta.get("xesam:artist") or []
        if isinstance(artist, list):
            artist = ", ".join(str(a) for a in artist)
        length_us = meta.get("mpris:length") or 0
        pos_us = _dbus_get(service, "org.mpris.MediaPlayer2.Player", "Position")
        status = _dbus_get(service, "org.mpris.MediaPlayer2.Player", "PlaybackStatus")
        return TrackInfo(
            player=player,
            title=str(meta.get("xesam:title") or ""),
            artist=str(artist or ""),
            album=str(meta.get("xesam:album") or ""),
            duration=float(length_us) / 1_000_000 if length_us else 0.0,
            position=float(pos_us) / 1_000_000 if pos_us else 0.0,
            status=str(status or "Stopped"),
            art_url=str(meta.get("mpris:artUrl") or ""),
        )
    except Exception:
        return None


# -------------------------------------------------------------- playerctl --
def _run_playerctl(*args: str, timeout: float = 2.0) -> str | None:
    if not shutil.which("playerctl"):
        return None
    try:
        r = subprocess.run(["playerctl", *args], capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except Exception:
        return None


def list_players_playerctl() -> list[str]:
    out = _run_playerctl("-l")
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def get_track_playerctl(player: str) -> TrackInfo | None:
    fmt = "{{title}}\x1f{{artist}}\x1f{{album}}\x1f{{mpris:length}}\x1f{{mpris:artUrl}}"
    out = _run_playerctl("--player", player, "metadata", "--format", fmt)
    if out is None:
        return None
    parts = out.split("\x1f")
    if len(parts) < 5 or not parts[0]:
        return None
    try:
        length_us = int(parts[3]) if parts[3] else 0
    except ValueError:
        length_us = 0
    pos_raw = _run_playerctl("--player", player, "position")
    try:
        pos = float(pos_raw) if pos_raw else 0.0
    except ValueError:
        pos = 0.0
    status = _run_playerctl("--player", player, "status") or "Stopped"
    return TrackInfo(
        player=player, title=parts[0], artist=parts[1], album=parts[2],
        duration=length_us / 1_000_000 if length_us else 0.0,
        position=pos, status=status.capitalize(), art_url=parts[4],
    )


# ------------------------------------------------------------ public API ---
def list_players() -> list[str]:
    """All MPRIS players, QtDBus first, playerctl as fallback."""
    players = list_players_dbus()
    if players:
        return players
    return list_players_playerctl()


def get_track(player: str) -> TrackInfo | None:
    track = get_track_dbus(player) if _dbus_available() else None
    if track is not None:
        return track
    return get_track_playerctl(player)


def play_pause(player: str) -> bool:
    if _dbus_available() and _dbus_call(
            f"org.mpris.MediaPlayer2.{player}",
            "org.mpris.MediaPlayer2.Player", "PlayPause"):
        return True
    return _run_playerctl("--player", player, "play-pause") is not None


def next_track(player: str) -> bool:
    if _dbus_available() and _dbus_call(
            f"org.mpris.MediaPlayer2.{player}",
            "org.mpris.MediaPlayer2.Player", "Next"):
        return True
    return _run_playerctl("--player", player, "next") is not None


def previous_track(player: str) -> bool:
    if _dbus_available() and _dbus_call(
            f"org.mpris.MediaPlayer2.{player}",
            "org.mpris.MediaPlayer2.Player", "Previous"):
        return True
    return _run_playerctl("--player", player, "previous") is not None


def seek(player: str, seconds: float) -> bool:
    """Absolute seek, seconds. MPRIS SetPosition needs a track id; use
    playerctl for the seek path (it handles the id internally), and fall
    back to MPRIS Seek (relative) if we know the current position."""
    if _run_playerctl("--player", player, "position", str(max(0.0, seconds))) is not None:
        return True
    if not _dbus_available():
        return False
    track = get_track_dbus(player)
    if track is None:
        return False
    delta_us = int((seconds - track.position) * 1_000_000)
    # SetPosition needs the /org/mpris/MediaPlayer2 track id; read it back.
    meta = _dbus_get(f"org.mpris.MediaPlayer2.{player}",
                     "org.mpris.MediaPlayer2.Player", "Metadata")
    track_id = meta.get("mpris:trackid") if isinstance(meta, dict) else None
    if track_id is None:
        return False
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusObjectPath
        bus = QDBusConnection.sessionBus()
        iface = QDBusInterface(f"org.mpris.MediaPlayer2.{player}",
                               "/org/mpris/MediaPlayer2",
                               "org.mpris.MediaPlayer2.Player", bus)
        reply = iface.call("SetPosition", QDBusObjectPath(str(track_id)),
                           int(seconds * 1_000_000))
        return not _is_error(reply)
    except Exception:
        return False
