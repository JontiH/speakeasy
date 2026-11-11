"""Desktop notifications that update one bubble in place.

On Windows, notifications go to the tray icon of the hotkey listener
(winlisten.py), which registers itself with set_sink(). Without the listener,
for example when run from a terminal, they only go to stderr.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

from .config import NOTIFY_ID_FILE, STATE_DIR, WINDOWS

_mode = "transient"
_sounds = False
# Windows: called as sink(msg, important, popup); popup=True asks for a tray pop-up.
_sink: Callable[[str, bool, bool], None] | None = None


def configure(cfg: dict) -> None:
    global _mode, _sounds
    out = cfg.get("output", {})
    _mode = out.get("notifications", "transient")
    _sounds = out.get("sounds", False)


def set_sink(sink: Callable[[str, bool, bool], None] | None) -> None:
    global _sink
    _sink = sink


# Two soft notes each: rising for start, falling for stop (Hz).
CHIMES = {"start": (660, 990), "stop": (990, 660)}
_chime_cache: dict[tuple[str, float], bytes] = {}


def chime(kind: str, volume: float = 0.15, rate: int = 22050) -> bytes:
    """A WAV in memory: two short sine notes, each fading in and out so there's no click."""
    key = (kind, volume)
    if key not in _chime_cache:
        import io
        import math
        import struct
        import wave

        frames = bytearray()
        note = int(0.06 * rate)  # 60 ms per note
        for freq in CHIMES[kind]:
            for i in range(note):
                envelope = math.sin(math.pi * i / note) ** 2  # smooth rise and fall
                frames += struct.pack("<h", int(32767 * volume * envelope * math.sin(2 * math.pi * freq * i / rate)))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(bytes(frames))
        _chime_cache[key] = buf.getvalue()
    return _chime_cache[key]


def _play(data: bytes) -> None:
    import winsound

    try:
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    except RuntimeError:  # no sound device
        pass


def cue(kind: str) -> None:
    """A soft chime when recording starts or stops (Windows, output.sounds).

    sounds = true (or a number from 0 to 1 for the volume, default 0.15), or false.
    """
    if not (WINDOWS and _sounds) or kind not in CHIMES:
        return
    volume = 0.15 if _sounds is True else max(0.0, min(1.0, float(_sounds)))
    data = chime(kind, volume)
    if kind == "start":
        _play(data)  # finish before ffmpeg opens the mic, so it isn't recorded
    else:
        import threading

        threading.Thread(target=_play, args=(data,), daemon=True).start()  # don't delay the text


def notify(msg: str, important: bool = False, final: bool = False) -> None:
    """Best-effort desktop + stderr notification.

    final: the last message of a run; it's closed a few seconds later.
    """
    if sys.stderr is not None:
        print(f"[speakeasy] {msg}", file=sys.stderr)
    if WINDOWS:
        # Windows keeps every tray pop-up in the Action Center, so in
        # "transient" mode only errors pop up; the icon shows the rest.
        if _sink is not None:
            _sink(msg, important, important or _mode == "normal")
        return
    if _mode == "off" and not important:
        return
    cmd = ["notify-send", "-t", "1500", "--print-id"]
    if _mode == "transient" and not important:
        cmd.append("--transient")
    # Start and stop run as separate processes, so the ID to replace is kept on disk.
    try:
        cmd += ["--replace-id", str(int(NOTIFY_ID_FILE.read_text()))]
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(cmd + ["Speakeasy", msg], capture_output=True, text=True, check=False)
    except FileNotFoundError:  # no notify-send
        return
    nid = result.stdout.strip()
    if result.returncode == 0 and nid.isdigit():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        NOTIFY_ID_FILE.write_text(nid)
        if final and not important:
            close_later(nid)


_CLOSER = """
import os, subprocess, sys, time
path, stamp, nid, seconds = sys.argv[1:]
time.sleep(float(seconds))
try:
    if os.stat(path).st_mtime_ns != int(stamp):
        sys.exit()
except OSError:
    sys.exit()
subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                "--object-path", "/org/freedesktop/Notifications",
                "--method", "org.freedesktop.Notifications.CloseNotification", nid],
               capture_output=True)
"""


def close_later(nid: str, seconds: int = 4) -> None:
    # GNOME doesn't always honour --transient, so close the bubble ourselves.
    # Skip it if the ID file was rewritten meanwhile: a new run owns the bubble.
    stamp = NOTIFY_ID_FILE.stat().st_mtime_ns
    subprocess.Popen(
        [sys.executable, "-c", _CLOSER, str(NOTIFY_ID_FILE), str(stamp), nid, str(seconds)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
