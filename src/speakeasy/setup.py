"""Per-machine setup: personal config, mic choice and hotkeys."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import wave
from pathlib import Path

import numpy as np

from . import config
from .config import USER_CONFIG

SILENT_DB = -60.0


def run(source: str | None) -> None:
    if source:
        import_personal(source)
    elif not USER_CONFIG.exists():
        USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        USER_CONFIG.write_text(
            "# Personal settings. Anything here overrides the repo's config.toml.\n"
            "# Tables merge key by key; lists and values replace the default.\n"
        )
        print(f"created {USER_CONFIG}")
    choose_mic()
    bind_hotkeys(config.load())


def import_personal(source: str) -> None:
    """Copy a personal config from a path or URL, keeping any existing one as .bak."""
    if re.match(r"https?://", source):
        with urllib.request.urlopen(source, timeout=30) as resp:
            data = resp.read()
    else:
        data = Path(source).expanduser().read_bytes()
    tomllib.loads(data.decode())  # refuse anything that isn't valid TOML
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if USER_CONFIG.exists():
        shutil.copy2(USER_CONFIG, USER_CONFIG.with_suffix(".toml.bak"))
    USER_CONFIG.write_bytes(data)
    print(f"personal config written to {USER_CONFIG}")


def list_sources() -> list[tuple[str, str]]:
    """(name, description) for each capture device, from PipeWire or PulseAudio."""
    if shutil.which("pw-dump"):
        nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True).stdout or "[]")
        found = []
        for node in nodes:
            props = node.get("info", {}).get("props", {})
            if props.get("media.class", "").startswith("Audio/Source") and props.get("node.name"):
                found.append((props["node.name"], props.get("node.description", props["node.name"])))
        return found
    if shutil.which("pactl"):
        lines = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
        names = [line.split("\t")[1] for line in lines.splitlines() if "\t" in line]
        return [(n, n) for n in names if not n.endswith(".monitor")]
    return []


def level_db(path: Path) -> float:
    """RMS level in dBFS, or -inf for an empty file."""
    try:
        with wave.open(str(path)) as fh:
            samples = np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2")
    except (OSError, EOFError, wave.Error):
        return float("-inf")
    if samples.size == 0:
        return float("-inf")
    rms = np.sqrt(np.mean((samples.astype(np.float64) / 32768) ** 2))
    return 20 * np.log10(rms) if rms > 0 else float("-inf")


def measure(sources: list[tuple[str, str]], backend: str, seconds: int = 4) -> list[float]:
    """Record from every source at once and return each one's level."""
    with tempfile.TemporaryDirectory() as tmp:
        procs = []
        for i, (name, _) in enumerate(sources):
            out = Path(tmp) / f"{i}.wav"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", backend, "-i", name,
                   "-t", str(seconds), "-ac", "1", "-ar", "16000", str(out)]
            procs.append((subprocess.Popen(cmd, stdin=subprocess.DEVNULL), out))
        for remaining in range(seconds, 0, -1):
            print(f"  {remaining}...", flush=True)
            time.sleep(1)
        levels = []
        for proc, out in procs:
            proc.wait(timeout=10)
            levels.append(level_db(out))
        return levels


def choose_mic() -> None:
    cfg = config.load()
    sources = list_sources()
    if not sources:
        print("no capture devices found; keeping audio.device as", cfg["audio"]["device"])
        return
    print("\nFinding your mic. Talk normally until the countdown ends.")
    levels = measure(sources, cfg["audio"]["backend"])
    best = max(range(len(sources)), key=lambda i: levels[i])
    for i, ((name, desc), db) in enumerate(zip(sources, levels), 1):
        print(f"  {i}. {db:6.1f} dB  {desc}  ({name})")
    if levels[best] < SILENT_DB:
        print("No mic heard you. Keeping audio.device =", cfg["audio"]["device"])
        return
    pick = best
    if sys.stdin.isatty():
        answer = input(f"Use {best + 1} ({sources[best][1]})? [Y/n/number] ").strip().lower()
        if answer.isdigit() and 1 <= int(answer) <= len(sources):
            pick = int(answer) - 1
        elif answer.startswith("n"):
            print("keeping audio.device =", cfg["audio"]["device"])
            return
    set_user_value("audio", "device", sources[pick][0])
    print(f"audio.device = {sources[pick][0]}  (in {USER_CONFIG})")


def set_user_value(section: str, key: str, value: str) -> None:
    """Set one string in the personal config, leaving the rest of the file as is."""
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text = USER_CONFIG.read_text() if USER_CONFIG.exists() else ""
    line = f"{key} = {json.dumps(value)}"
    header = re.search(rf"^\[{re.escape(section)}\][ \t]*$", text, flags=re.MULTILINE)
    if header is None:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"[{section}]\n{line}\n"
    else:
        nxt = re.search(r"^\[", text[header.end():], flags=re.MULTILINE)
        end = header.end() + nxt.start() if nxt else len(text)
        body = text[header.end():end]
        existing = re.search(rf"^{re.escape(key)}\s*=.*$", body, flags=re.MULTILINE)
        if existing:
            body = body[: existing.start()] + line + body[existing.end():]
        else:
            body = "\n" + line + body
        text = text[: header.end()] + body + text[end:]
    tomllib.loads(text)
    USER_CONFIG.write_text(text)


MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEYBINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/{}/"


def bind_hotkeys(cfg: dict) -> None:
    keys = cfg.get("hotkeys", {})
    command = str(config.REPO / "speakeasy")
    wanted = [
        ("speakeasy", "Speakeasy: start/stop dictation", command, keys.get("toggle")),
        ("speakeasy-cancel", "Speakeasy: cancel dictation", f"{command} --cancel", keys.get("cancel")),
    ]
    if "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", "") or not shutil.which("gsettings"):
        print("\nNot GNOME, so bind these yourself in your desktop's shortcut settings:")
        for _, name, command, binding in wanted:
            if binding:
                print(f"  {binding:20} {command}")
        return
    for path_id, name, command, binding in wanted:
        if binding:
            _gnome_bind(path_id, name, command, binding)
            print(f"bound {binding} to {name}")


def _gnome_bind(path_id: str, name: str, command: str, binding: str) -> None:
    path = KEYBINDING_PATH.format(path_id)
    current = subprocess.run(["gsettings", "get", MEDIA_KEYS, "custom-keybindings"],
                             capture_output=True, text=True).stdout.strip()
    # "@as []" is how gsettings prints an empty list.
    paths = [] if current.startswith("@") else ast.literal_eval(current)
    if path not in paths:
        paths.append(path)  # keep the user's other shortcuts
        subprocess.run(["gsettings", "set", MEDIA_KEYS, "custom-keybindings", str(paths)], check=True)
    schema = f"{MEDIA_KEYS}.custom-keybinding:{path}"
    # gsd-media-keys doesn't retry a key it failed to grab (e.g. while another
    # shortcut held it), but clearing and setting the binding makes it try again.
    subprocess.run(["gsettings", "set", schema, "binding", ""], check=True)
    for key, value in (("name", name), ("command", command), ("binding", binding)):
        subprocess.run(["gsettings", "set", schema, key, value], check=True)
