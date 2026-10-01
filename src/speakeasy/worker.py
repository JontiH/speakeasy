"""Background process that keeps the model loaded between dictations.

The first hotkey press starts it, so the model loads while you talk. It
answers on a Unix socket and exits after keep_loaded_minutes without a
request. If it's missing or fails, the hotkey process transcribes itself.
"""
from __future__ import annotations

import fcntl
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

from . import config, engines, recorder
from .config import WORKER_LOCK, WORKER_LOG, WORKER_SOCKET, mark
from .streaming import Streamer
from .text import clean


def request(req: dict, timeout: float) -> dict | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(WORKER_SOCKET))
        sock.sendall(json.dumps(req).encode() + b"\n")
        return json.loads(_read_line(sock))
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def _read_line(sock: socket.socket) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def _take_lock():
    """The open lock file if this process got the lock, else None."""
    WORKER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = WORKER_LOCK.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        fh.close()
        return None


def ensure_running(cfg: dict) -> None:
    if cfg.get("keep_loaded_minutes", 0) <= 0:
        return
    lock = _take_lock()
    if lock is None:  # a worker holds it
        return
    lock.close()
    with WORKER_LOG.open("a") as log:
        subprocess.Popen(
            [str(config.REPO / "speakeasy"), "--serve"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )


def transcribe(wav: Path) -> str | None:
    if not WORKER_SOCKET.exists():
        return None
    resp = request({"cmd": "transcribe", "wav": str(wav)}, timeout=120)
    if resp is None or "error" in resp:
        mark(f"worker failed: {resp}")
        return None
    return resp["text"]


def stop() -> bool:
    return request({"cmd": "quit"}, timeout=5) is not None


def serve() -> None:
    config.set_role("worker: ")
    lock = _take_lock()
    if lock is None:
        return
    WORKER_SOCKET.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(WORKER_SOCKET))
    srv.listen(4)
    streamer = Streamer(config.WAV_FILE, recorder.recording_pid, config.load, engines.recognize_samples)
    try:
        cfg = config.load()
        mark("started")
        try:
            engines.preload(cfg)
        except Exception as exc:  # the first request reports it properly
            mark(f"preload failed: {exc}")
        threading.Thread(target=streamer.run, daemon=True).start()
        while True:
            srv.settimeout(cfg.get("keep_loaded_minutes", 10) * 60)
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                if recorder.is_recording():
                    continue
                mark("idle, exiting")
                return
            with conn:
                if not _handle(conn, streamer):
                    mark("stopped by request")
                    return
            cfg = config.load()
    finally:
        WORKER_SOCKET.unlink(missing_ok=True)
        srv.close()


def _handle(conn: socket.socket, streamer: Streamer) -> bool:
    """Answer one request. False means the worker should exit."""
    config.reset_clock()
    conn.settimeout(5)
    try:
        req = json.loads(_read_line(conn))
        if req.get("cmd") == "quit":
            conn.sendall(b'{"ok": true}\n')
            return False
        # Re-read so config edits apply without restarting the worker.
        cfg, wav = config.load(), Path(req["wav"])
        try:
            streamed = streamer.finish(wav)
        except Exception as exc:
            streamed = None
            mark(f"stream finish failed: {type(exc).__name__}: {exc}")
        text = clean(cfg, streamed) if streamed is not None else engines.transcribe(cfg, wav)
        mark(f"transcribed ({len(text)} chars)")
        reply = {"text": text}
    except Exception as exc:
        reply = {"error": f"{type(exc).__name__}: {exc}"}
    conn.sendall(json.dumps(reply).encode() + b"\n")
    return True
