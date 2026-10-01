"""Transcribe a recording in chunks while it's still being recorded."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .config import mark
from .engines import MODEL_LOCK, SAMPLE_RATE


def wav_data_offset(path: Path) -> int | None:
    # ffmpeg adds a LIST chunk, so the samples don't start at the usual byte 44.
    with path.open("rb") as fh:
        head = fh.read(512)
    i = head.find(b"data")
    return None if i < 0 else i + 8


def quietest_cut(audio: np.ndarray, lo: int, hi: int) -> int:
    """Sample index in the middle of the quietest 300 ms between lo and hi."""
    win = int(0.3 * SAMPLE_RATE)
    power = np.cumsum(audio[lo:hi].astype(np.float64) ** 2)
    energy = power[win:] - power[:-win]
    return lo + int(np.argmin(energy)) + win // 2


class Streamer:
    """Chunked transcription of the WAV that ffmpeg is still writing.

    ffmpeg appends raw 16-bit samples as it goes, so new audio is just the
    bytes past the last read. Once enough has built up, it's cut at the
    quietest point and transcribed, leaving only the tail for stop time.
    """

    def __init__(
        self,
        wav: Path,
        recording_pid: Callable[[], int | None],
        load_config: Callable[[], dict],
        recognize: Callable[[dict, np.ndarray], str],
    ):
        self.wav = wav
        self.recording_pid = recording_pid
        self.load_config = load_config
        self.recognize = recognize
        self.pid = None
        self.active = False

    def start(self, pid: int, cfg: dict) -> None:
        self.pid = pid
        self.cfg = cfg
        self.active = cfg.get("engine") == "parakeet" and cfg["parakeet"].get("stream", True)
        self.inode = None
        self.offset = 0
        self.pending = np.zeros(0, dtype=np.int16)
        self.texts: list[str] = []

    def read_new(self) -> None:
        try:
            st = self.wav.stat()
        except FileNotFoundError:
            return
        if self.inode is None:
            offset = wav_data_offset(self.wav)
            if offset is None:
                return
            self.inode, self.offset = st.st_ino, offset
        elif st.st_ino != self.inode or st.st_size < self.offset:
            # A new file can reuse the old inode number, but not be shorter
            # than what's already been read from the old one.
            self.active = False
            return
        with self.wav.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        data = data[: len(data) // 2 * 2]
        self.offset += len(data)
        self.pending = np.concatenate([self.pending, np.frombuffer(data, dtype="<i2")])

    def poll(self) -> None:
        pid = self.recording_pid()
        if pid is None:
            return  # stopped or cancelled; keep the chunks for the stop request
        with MODEL_LOCK:
            if pid != self.pid:
                self.start(pid, self.load_config())
            if not self.active:
                return
            self.read_new()
            # Cut 2 s short of the model's limit, at the quietest point in the back half.
            hi = int((self.cfg["parakeet"].get("max_chunk_seconds", 20) - 2) * SAMPLE_RATE)
            while self.active and len(self.pending) >= hi:
                cut = quietest_cut(self.pending, hi // 2, hi)
                self.texts.append(self.recognize(self.cfg, self.pending[:cut]))
                self.pending = self.pending[cut:]
                mark(f"streamed chunk {len(self.texts)} ({cut / SAMPLE_RATE:.1f}s)")

    def finish(self, wav: Path) -> str | None:
        """Raw text for the whole recording, or None if it wasn't streamed."""
        with MODEL_LOCK:
            if not self.active or wav != self.wav:
                mark(f"not streamed (active={self.active})")
                return None
            self.read_new()  # ffmpeg has exited, so this is the rest of the file
            done, self.active = self.active, False
            if not done or self.inode is None:
                mark("not streamed (recording file changed)")
                return None
            if len(self.pending) > SAMPLE_RATE // 5:
                self.texts.append(self.recognize(self.cfg, self.pending))
            mark(f"streamed tail ({len(self.pending) / SAMPLE_RATE:.1f}s, {len(self.texts)} chunks)")
            return " ".join(t.strip() for t in self.texts if t.strip())

    def run(self, interval: float = 0.5) -> None:
        while True:
            time.sleep(interval)
            try:
                self.poll()
            except Exception as exc:  # the stop request falls back to the whole file
                self.active = False
                mark(f"stream error: {type(exc).__name__}: {exc}")
