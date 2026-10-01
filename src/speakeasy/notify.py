"""Desktop notifications that update one bubble in place."""
from __future__ import annotations

import subprocess
import sys

from .config import NOTIFY_ID_FILE, STATE_DIR

_mode = "transient"


def configure(cfg: dict) -> None:
    global _mode
    _mode = cfg.get("output", {}).get("notifications", "transient")


def notify(msg: str, important: bool = False, final: bool = False) -> None:
    """Best-effort desktop + stderr notification.

    final: the last message of a run; it's closed a few seconds later.
    """
    print(f"[speakeasy] {msg}", file=sys.stderr)
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
