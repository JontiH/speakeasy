"""Mic capture with ffmpeg. Start and stop happen in separate processes."""
from __future__ import annotations

import os
import re
import shutil
import signal
import struct
import subprocess
import time
from pathlib import Path

from .config import PID_FILE, STATE_DIR, WAV_FILE, WINDOWS

if WINDOWS:
    from . import win32


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    if WINDOWS:
        # winget puts ffmpeg here, but processes started before the install
        # (like the tray listener) don't have it on PATH yet.
        linked = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        if linked.exists():
            return str(linked)
    return "ffmpeg"


def dshow_devices() -> list[str]:
    """Names of the DirectShow audio capture devices, as ffmpeg lists them."""
    try:
        listing = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=win32.CREATE_NO_WINDOW if WINDOWS else 0,
        ).stderr
    except (OSError, subprocess.TimeoutExpired):
        return []
    return re.findall(r'\]\s+"(.+)"\s+\(audio\)', listing)


def pick_default(names: list[str], windows_default: str | None, ignore: list[str]) -> str:
    """The mic "default" means: Windows' default, unless it matches an ignore
    pattern (like a webcam), else the first mic that doesn't match one."""
    if not names:
        raise RuntimeError("ffmpeg found no microphone")

    def ignored(name: str) -> bool:
        return any(p.lower() in name.lower() for p in ignore)

    if windows_default in names and not ignored(windows_default):
        return windows_default
    return next((n for n in names if not ignored(n)), names[0])


def input_args(audio: dict, device: str | None = None) -> list[str]:
    """ffmpeg arguments that open the configured mic."""
    backend, device = audio["backend"], device or audio["device"]
    if backend != "dshow":
        return ["-f", backend, "-i", device]
    if device == "default":  # DirectShow has no default device
        device = pick_default(dshow_devices(), win32.default_capture_name() if WINDOWS else None,
                              audio.get("ignore_devices", []))
    # dshow buffers 500 ms by default, which would all be lost on stop.
    return ["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={device}"]


def _alive(pid: int) -> bool:
    if WINDOWS:
        return win32.pid_alive(pid, "ffmpeg")
    os.kill(pid, 0)
    return True


def recording_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if _alive(pid) else None
    except (OSError, ValueError):
        return None


def is_recording() -> bool:
    if recording_pid() is not None:
        return True
    PID_FILE.unlink(missing_ok=True)
    return False


def start(cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WAV_FILE.unlink(missing_ok=True)
    audio = cfg["audio"]
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        *input_args(audio),
        "-t", str(audio["max_seconds"]),
        "-ac", "1", "-ar", "16000",  # mono 16 kHz, what both engines expect
        # Without this ffmpeg writes in large blocks, and the worker can't read
        # the recording while it's still going.
        "-flush_packets", "1",
        str(WAV_FILE),
    ]
    # On Windows ffmpeg gets a console with no window, so stop() can send it Ctrl+C.
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            creationflags=win32.CREATE_NO_WINDOW if WINDOWS else 0)
    PID_FILE.write_text(str(proc.pid))


def stop() -> None:
    """Stop ffmpeg and wait until it has finished writing the file."""
    pid = recording_pid()
    PID_FILE.unlink(missing_ok=True)
    if pid is None:
        return
    if WINDOWS:
        # Ctrl+C lets ffmpeg finalize the WAV. If it doesn't stop, kill it and
        # write the header sizes ourselves.
        if not (win32.send_ctrl_c(pid) and win32.wait_exit(pid, 5)):
            win32.terminate(pid)
            win32.wait_exit(pid, 2)
        fix_wav_header(WAV_FILE)
        return
    os.kill(pid, signal.SIGINT)  # SIGINT lets ffmpeg finalize the WAV
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def cancel() -> bool:
    """Stop ffmpeg and delete the recording. False if nothing was recording."""
    pid = recording_pid()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return False
    PID_FILE.unlink(missing_ok=True)
    if WINDOWS:
        win32.terminate(pid)
        win32.wait_exit(pid, 2)  # Windows can't delete a file that's still open
    else:
        os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        try:
            WAV_FILE.unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(0.05)
    return True


def fix_wav_header(path: Path) -> None:
    """Set the RIFF and data sizes from the file length.

    ffmpeg writes them when it exits cleanly; a killed ffmpeg leaves
    placeholders, and the wave module then sees no samples.
    """
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            head = fh.read(512)
            data = head.find(b"data")
            if not head.startswith(b"RIFF") or data < 0:
                return
            riff_size, data_size = size - 8, size - data - 8
            if struct.unpack_from("<I", head, 4)[0] == riff_size and \
                    struct.unpack_from("<I", head, data + 4)[0] == data_size:
                return
            fh.seek(4)
            fh.write(struct.pack("<I", riff_size))
            fh.seek(data + 4)
            fh.write(struct.pack("<I", data_size))
    except OSError:
        pass
