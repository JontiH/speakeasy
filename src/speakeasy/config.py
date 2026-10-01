"""Paths, config loading and the timing log."""
from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # this file is src/speakeasy/config.py
MODEL_DIR = REPO / "models"
DEFAULT_CONFIG = REPO / "config.toml"
USER_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "speakeasy" / "config.toml"
)

# XDG_RUNTIME_DIR is a per-user tmpfs that's cleared on logout.
STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/speakeasy-{os.getuid()}") / "speakeasy"
PID_FILE = STATE_DIR / "record.pid"
WAV_FILE = STATE_DIR / "record.wav"
LAST_WAV = STATE_DIR / "last.wav"  # kept for --compare
NOTIFY_ID_FILE = STATE_DIR / "notify.id"
TIMING_LOG = STATE_DIR / "timing.log"
WORKER_SOCKET = STATE_DIR / "worker.sock"
WORKER_LOG = STATE_DIR / "worker.log"
WORKER_LOCK = STATE_DIR / "worker.lock"


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
    with TIMING_LOG.open("a") as fh:
        fh.write(f"{time.strftime('%H:%M:%S')} {time.time() - _t0:6.2f}s {_role}{stage}\n")
