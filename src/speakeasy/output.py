"""Getting text into the focused window on GNOME Wayland."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import mark
from .notify import notify

# Ubuntu's ydotoold 0.1.8 always listens here; 1.x reads YDOTOOL_SOCKET.
YDOTOOL_SOCKET = Path(os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket"))


def _ydotool_delay() -> str:
    # Without ydotoold, each call makes a fresh uinput device and GNOME drops
    # the first keys while it registers it.
    return "0" if YDOTOOL_SOCKET.exists() else "400"


def type_text(text: str, key_delay_ms: int) -> bool:
    # wtype can't be used: Mutter has no virtual-keyboard protocol.
    # Text goes on stdin so a leading "-" isn't read as a flag.
    cmd = ["ydotool", "type", "--key-delay", str(key_delay_ms), "--delay", _ydotool_delay(), "--file", "-"]
    return subprocess.run(cmd, input=text.encode(), capture_output=True, check=False).returncode == 0


def press_keys(combo: str) -> bool:
    # ydotool 0.1.8 exits 0 on unknown key names, so a typo fails silently.
    cmd = ["ydotool", "key", "--delay", _ydotool_delay(), combo]
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def focused_window() -> dict | None:
    """App and title of the focused window, from the focused-window GNOME extension."""
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
             "--object-path", "/org/gnome/Shell/FocusedWindow",
             "--method", "org.gnome.Shell.FocusedWindow.Get"],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(ast.literal_eval(result.stdout.strip())[0])
    except (ValueError, SyntaxError, IndexError):
        return None


def window_matches(win: dict | None, patterns: list[str], keys: tuple[str, ...]) -> bool:
    if win is None:
        return False
    haystack = " ".join(win.get(k, "") for k in keys).lower()
    return any(p.lower() in haystack for p in patterns)


def emit(cfg: dict, text: str) -> None:
    out = cfg["output"]
    mode = out["mode"]
    if mode == "stdout":
        sys.stdout.write(text)
        return
    # Every other mode copies too, so a failed type never loses the text.
    subprocess.run(["wl-copy"], input=text.encode(), check=False)
    if mode == "clipboard":
        return

    enter_only_in = out.get("enter_only_in", [])
    paste_in = out.get("paste_in", [])
    win = focused_window() if enter_only_in or paste_in else None
    mark(f"focused window: {win}")
    paste = window_matches(win, paste_in, ("wm_class", "app_id"))
    ok = press_keys(out.get("paste_keys", "ctrl+shift+v")) if paste else type_text(text, out.get("key_delay_ms", 4))
    mark("pasted" if paste else "typed")
    if not ok:
        notify("typing failed, text is on the clipboard", important=True)
        return
    if not out.get("press_enter", False):
        return
    if enter_only_in and not window_matches(win, enter_only_in, ("title", "wm_class", "app_id")):
        notify("not sent: focused window isn't in enter_only_in", final=True)
        return
    if paste:
        # The terminal fetches the clipboard asynchronously; an immediate
        # Enter can land before the pasted text.
        time.sleep(out.get("paste_enter_delay_ms", 150) / 1000)
    press_keys("Enter")
