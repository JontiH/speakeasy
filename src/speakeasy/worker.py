"""Background process that keeps the model loaded between dictations.

The first hotkey press starts it, so the model loads while you talk. It
answers on a Unix socket and exits after keep_loaded_minutes without a
request. If it's missing or fails, the hotkey process transcribes itself.

Windows Python has no AF_UNIX, so there it listens on a random localhost
port. WORKER_SOCKET then holds "<port> <token>", and every request must carry
the token, so other users on the machine can't talk to it.
"""
from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path

from . import config, engines, recorder
from .config import WINDOWS, WORKER_LOCK, WORKER_LOG, WORKER_SOCKET, mark
from .streaming import Streamer
from .text import clean

if WINDOWS:
    from . import win32
else:
    import fcntl


def _connect(timeout: float) -> tuple[socket.socket, str]:
    """A connected socket and the token to send with requests."""
    if not WINDOWS:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(WORKER_SOCKET))
        except OSError:
            sock.close()
            raise
        return sock, ""
    try:
        port, token = WORKER_SOCKET.read_text().split()
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
    except ValueError as exc:
        raise OSError(f"bad {WORKER_SOCKET.name}") from exc
    sock.settimeout(timeout)
    return sock, token


def request(req: dict, timeout: float) -> dict | None:
    try:
        sock, token = _connect(timeout)
    except OSError:
        return None
    try:
        if token:
            req = {**req, "token": token}
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
    return take_lock(WORKER_LOCK)


def take_lock(path: Path):
    """The open lock file if this process got the lock, else None."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a")  # "a", not "w": don't truncate a file another process has locked
    if WINDOWS:
        if win32.try_lock(fh):
            return fh
        fh.close()
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        fh.close()
        return None


def release_lock(fh) -> None:
    if WINDOWS:
        win32.unlock(fh)
    fh.close()


def ensure_running(cfg: dict) -> None:
    if cfg.get("keep_loaded_minutes", 0) <= 0:
        return
    lock = _take_lock()
    if lock is None:  # a worker holds it
        return
    release_lock(lock)
    cmd, env = config.self_command("--serve", console=False)
    with WORKER_LOG.open("a") as log:
        if WINDOWS:
            subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=win32.detached_flags())
        else:
            subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)


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
    srv, token = _listen()
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
                if not _handle(conn, streamer, token):
                    mark("stopped by request")
                    return
            cfg = config.load()
    finally:
        WORKER_SOCKET.unlink(missing_ok=True)
        srv.close()


def _listen() -> tuple[socket.socket, str]:
    """The listening socket, and the token clients must send ("" on Linux)."""
    if not WINDOWS:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(WORKER_SOCKET))
        srv.listen(4)
        return srv, ""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    token = secrets.token_hex(16)
    tmp = WORKER_SOCKET.with_suffix(".tmp")
    tmp.write_text(f"{srv.getsockname()[1]} {token}")
    tmp.replace(WORKER_SOCKET)  # clients never see a half-written file
    return srv, token


def _handle(conn: socket.socket, streamer: Streamer, token: str = "") -> bool:
    """Answer one request. False means the worker should exit."""
    config.reset_clock()
    conn.settimeout(5)
    try:
        req = json.loads(_read_line(conn))
        if token and not secrets.compare_digest(str(req.get("token", "")), token):
            conn.sendall(b'{"error": "bad token"}\n')
            return True
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
