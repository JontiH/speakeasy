"""Mic capture with ffmpeg. Start and stop happen in separate processes."""
from __future__ import annotations

import os
import signal
import subprocess
import time

from .config import PID_FILE, STATE_DIR, WAV_FILE


def recording_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
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
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", audio["backend"], "-i", audio["device"],
        "-t", str(audio["max_seconds"]),
        "-ac", "1", "-ar", "16000",  # mono 16 kHz, what both engines expect
        # Without this ffmpeg writes in large blocks, and the worker can't read
        # the recording while it's still going.
        "-flush_packets", "1",
        str(WAV_FILE),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    PID_FILE.write_text(str(proc.pid))


def stop() -> None:
    """Stop ffmpeg and wait until it has finished writing the file."""
    pid = recording_pid()
    PID_FILE.unlink(missing_ok=True)
    if pid is None:
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
    os.kill(pid, signal.SIGTERM)
    WAV_FILE.unlink(missing_ok=True)
    return True
