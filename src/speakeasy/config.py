"""Paths, config loading and the timing log."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path

WINDOWS = sys.platform == "win32"

REPO = Path(__file__).resolve().parents[2]  # this file is src/speakeasy/config.py
MODEL_DIR = REPO / "models"
DEFAULT_CONFIG = REPO / "config.toml"
# Merged over config.toml on Windows: dshow capture, Windows app names, Ctrl+V.
WINDOWS_CONFIG = REPO / "config.windows.toml"


def _config_home() -> Path:
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"])
    if WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path.home() / ".config"


def _state_home() -> Path:
    if os.environ.get("XDG_RUNTIME_DIR"):
        # A per-user tmpfs that's cleared on logout.
        return Path(os.environ["XDG_RUNTIME_DIR"])
    if WINDOWS:
        return Path(tempfile.gettempdir())  # %LOCALAPPDATA%\Temp, per user
    return Path(f"/tmp/speakeasy-{os.getuid()}")


USER_CONFIG = _config_home() / "speakeasy" / "config.toml"
STATE_DIR = _state_home() / "speakeasy"
PID_FILE = STATE_DIR / "record.pid"
WAV_FILE = STATE_DIR / "record.wav"
LAST_WAV = STATE_DIR / "last.wav"  # kept for --compare
NOTIFY_ID_FILE = STATE_DIR / "notify.id"
TIMING_LOG = STATE_DIR / "timing.log"
# Windows Python has no AF_UNIX, so there the worker listens on localhost TCP
# and this file holds "<port> <token>" instead of being a socket.
WORKER_SOCKET = STATE_DIR / ("worker.addr" if WINDOWS else "worker.sock")
WORKER_LOG = STATE_DIR / "worker.log"
WORKER_LOCK = STATE_DIR / "worker.lock"
# Windows only: the hotkey listener and tray icon.
LISTENER_LOCK = STATE_DIR / "listener.lock"
LISTENER_LOG = STATE_DIR / "listener.log"


def self_command(*args: str, console: bool = True) -> tuple[list[str], dict]:
    """Command and environment that run speakeasy again with these arguments.

    console=False on Windows uses pythonw.exe, so no console window appears.
    """
    env = dict(os.environ)
    if not WINDOWS:
        return [str(REPO / "speakeasy"), *args], env
    env["PYTHONPATH"] = str(REPO / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    exe = Path(sys.executable)
    if not console and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), "-m", "speakeasy", *args], env


def merge(base: dict, override: dict) -> dict:
    """Tables merge key by key; any other value in override replaces base's."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    with DEFAULT_CONFIG.open("rb") as fh:
        cfg = tomllib.load(fh)
    if WINDOWS and WINDOWS_CONFIG.exists():
        with WINDOWS_CONFIG.open("rb") as fh:
            cfg = merge(cfg, tomllib.load(fh))
    if USER_CONFIG.exists():
        with USER_CONFIG.open("rb") as fh:
            cfg = merge(cfg, tomllib.load(fh))
    return cfg


_t0 = time.time()
_role = ""


def set_role(role: str) -> None:
    global _role
    _role = role


def reset_clock() -> None:
    global _t0
    _t0 = time.time()


def mark(stage: str) -> None:
    """Append a timestamped line to the timing log."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with TIMING_LOG.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"{time.strftime('%H:%M:%S')} {time.time() - _t0:6.2f}s {_role}{stage}\n")
