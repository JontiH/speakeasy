"""Speech-to-text engines. Models are cached so the worker loads each once."""
from __future__ import annotations

import functools
import threading
import wave
from pathlib import Path

from .config import MODEL_DIR, mark
from .notify import notify
from .text import clean

SAMPLE_RATE = 16000
# Held while a model runs: the worker's streaming thread and requests share it.
MODEL_LOCK = threading.RLock()

_loaded: dict[tuple, object] = {}


def _cached(key: tuple, load):
    if key not in _loaded:
        _loaded.clear()  # one engine in memory at a time
        _loaded[key] = load()
        mark(f"{key[0]} loaded")
    return _loaded[key]


def load_whisper(cfg: dict):
    w = cfg["whisper"]
    return _cached(("whisper", w["size"], w["compute_type"]), lambda: _load_whisper(w))


def _load_whisper(w: dict):
    from faster_whisper import WhisperModel

    MODEL_DIR.mkdir(exist_ok=True)
    kwargs = dict(device="cpu", compute_type=w["compute_type"], download_root=str(MODEL_DIR))
    # Without local_files_only, every load asks huggingface.co for updates.
    try:
        return WhisperModel(w["size"], local_files_only=True, **kwargs)
    except Exception:
        notify(f"downloading whisper model '{w['size']}'...")
        return WhisperModel(w["size"], **kwargs)


def transcribe_whisper(cfg: dict, wav: Path) -> str:
    w = cfg["whisper"]
    lang = None if w["language"] == "auto" else w["language"]
    words = w.get("vocabulary", [])
    prompt = ("Glossary: " + ", ".join(words) + ".") if words else None
    segments, _info = load_whisper(cfg).transcribe(
        str(wav), language=lang, vad_filter=w.get("vad_filter", False), initial_prompt=prompt
    )
    return "".join(seg.text for seg in segments)


def parakeet_dir(cfg: dict) -> Path:
    return MODEL_DIR / cfg["parakeet"]["model"].removeprefix("nemo-")


def load_parakeet(cfg: dict):
    p = cfg["parakeet"]
    return _cached(("parakeet", p["model"], p.get("quantization")), lambda: _load_parakeet(cfg))


def _load_parakeet(cfg: dict):
    import onnx_asr

    p = cfg["parakeet"]
    # onnx-asr downloads into the directory only if it doesn't exist yet.
    if not parakeet_dir(cfg).exists():
        notify(f"downloading {p['model']}...")
    return onnx_asr.load_model(p["model"], parakeet_dir(cfg), quantization=p.get("quantization") or None)


@functools.cache
def load_vad():
    import onnx_asr

    return onnx_asr.load_vad("silero", MODEL_DIR / "silero-vad")


def transcribe_parakeet(cfg: dict, wav: Path) -> str:
    model = load_parakeet(cfg)
    max_chunk = cfg["parakeet"].get("max_chunk_seconds", 20)
    with wave.open(str(wav)) as fh:
        seconds = fh.getnframes() / fh.getframerate()
    if seconds <= max_chunk:
        return model.recognize(str(wav))
    # Merge speech up to max_chunk and split only at the pause closest to it.
    # The defaults split at every 100 ms gap, which breaks sentences apart.
    chunked = model.with_vad(
        load_vad(),
        max_speech_duration_s=max_chunk,
        min_silence_duration_ms=max_chunk * 1000,
        speech_pad_ms=200,
        threshold=0.35,
    )
    return " ".join(seg.text.strip() for seg in chunked.recognize(str(wav)))


def recognize_samples(cfg: dict, samples) -> str:
    """Parakeet on 16 kHz int16 samples already in memory."""
    return load_parakeet(cfg).recognize(samples.astype("float32") / 32768, sample_rate=SAMPLE_RATE)


ENGINES = {"whisper": transcribe_whisper, "parakeet": transcribe_parakeet}
LOADERS = {"whisper": load_whisper, "parakeet": load_parakeet}


def engine_name(cfg: dict) -> str:
    engine = cfg.get("engine", "parakeet")
    if engine not in ENGINES:
        raise SystemExit(f"unknown engine {engine!r}, expected one of {sorted(ENGINES)}")
    return engine


def preload(cfg: dict) -> None:
    LOADERS[engine_name(cfg)](cfg)


def transcribe(cfg: dict, wav: Path) -> str:
    """Whole-file transcription, cleaned up."""
    with MODEL_LOCK:
        return clean(cfg, ENGINES[engine_name(cfg)](cfg, wav))


def download_all(cfg: dict) -> None:
    print(f"whisper '{cfg['whisper']['size']}'...")
    load_whisper(cfg)
    print(f"parakeet '{cfg['parakeet']['model']}' and the Silero VAD...")
    load_parakeet(cfg)
    load_vad()
    print("Models ready.")
