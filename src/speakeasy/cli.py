"""Command line. With no arguments, toggles recording."""
from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

from . import config, engines, notify as notify_mod, output, recorder, worker
from .config import LAST_WAV, WAV_FILE, mark
from .notify import notify


def start(cfg: dict) -> None:
    recorder.start(cfg)
    notify("recording... (run again to stop)")
    worker.ensure_running(cfg)


def stop_and_transcribe(cfg: dict) -> None:
    recorder.stop()
    mark("recording stopped")
    if not WAV_FILE.exists() or WAV_FILE.stat().st_size < 1024:
        notify("no audio captured", final=True)
        return

    notify("transcribing...")
    text = worker.transcribe(WAV_FILE)
    if text is None:
        mark("worker unavailable, transcribing in-process")
        text = engines.transcribe(cfg, WAV_FILE)
    WAV_FILE.replace(LAST_WAV)
    mark(f"transcribed ({engines.engine_name(cfg)}, {len(text)} chars)")

    text = text.strip()
    if not text:
        notify("(no speech detected)", final=True)
        return
    out = cfg["output"]
    if out.get("trailing_space", True) and not out.get("press_enter", False):
        text += " "
    output.emit(cfg, text)
    notify(f"done: {text.strip()[:60]}", final=True)


def cancel() -> None:
    if recorder.cancel():
        mark("cancelled")
        notify("cancelled, recording discarded", final=True)
    else:
        notify("nothing to cancel", final=True)


def show_info(cfg: dict) -> None:
    from importlib.metadata import version

    engine, out = engines.engine_name(cfg), cfg["output"]
    print(f"engine:         {engine}")
    if engine == "parakeet":
        p = cfg["parakeet"]
        files = engines.parakeet_dir(cfg)
        print(f"model:          {p['model']}")
        print(f"compute:        cpu, {p.get('quantization') or 'full precision'}")
        print(f"model files:    {files if files.exists() else 'not downloaded'}")
        print(f"streaming:      {'on' if p.get('stream', True) else 'off'}")
        print(f"onnx-asr:       {version('onnx-asr')}")
        print(f"onnxruntime:    {version('onnxruntime')}")
    else:
        from faster_whisper.utils import _MODELS

        w = cfg["whisper"]
        repo = _MODELS.get(w["size"], w["size"])
        cached = sorted(config.MODEL_DIR.glob(f"models--{repo.replace('/', '--')}/snapshots/*/model.bin"))
        print(f"model:          {w['size']} ({repo})")
        print(f"compute:        cpu, {w['compute_type']}")
        print(f"language:       {w['language']}")
        print(f"model files:    {cached[-1].parent if cached else 'not downloaded'}")
        print(f"faster-whisper: {version('faster-whisper')}")
        print(f"ctranslate2:    {version('ctranslate2')}")
    print(f"mic:            {cfg['audio']['device']}")
    print(f"output:         {out['mode']}, press_enter={out.get('press_enter', False)}")
    print(f"keep loaded:    {cfg.get('keep_loaded_minutes', 0)} min")
    print(f"worker:         {'running' if config.WORKER_SOCKET.exists() else 'not running'}")
    print(f"ydotoold:       {'running' if output.YDOTOOL_SOCKET.exists() else 'not running'}")
    print(f"personal config: {config.USER_CONFIG if config.USER_CONFIG.exists() else 'none'}")
    print(f"recording now:  {'yes' if recorder.is_recording() else 'no'}")


def compare(cfg: dict, wav: Path) -> None:
    if not wav.exists():
        raise SystemExit(f"no recording at {wav}. Dictate something first.")
    with wave.open(str(wav)) as fh:
        print(f"{wav}: {fh.getnframes() / fh.getframerate():.1f}s of audio\n")
    for engine in engines.ENGINES:
        began = time.time()
        text = engines.transcribe({**cfg, "engine": engine}, wav).strip()
        print(f"== {engine} ({time.time() - began:.1f}s, including model load)\n{text}\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="speakeasy", description="Local voice dictation. Run once to start recording, again to stop and type the text.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cancel", action="store_true", help="stop recording and throw the audio away")
    group.add_argument("--setup", nargs="?", const="", metavar="FROM",
                       help="pick the mic and bind hotkeys; FROM is an optional path or URL of a personal config to install first")
    group.add_argument("--download", action="store_true", help="download every engine's model")
    group.add_argument("--compare", nargs="?", const=str(LAST_WAV), metavar="WAV",
                       help="run a recording (default: the last one) through every engine")
    group.add_argument("-v", "--info", action="store_true", help="show engine, model, mic and status")
    group.add_argument("--stop-worker", action="store_true", help="unload the model now (needed after code changes)")
    group.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.setup is not None:
        from . import setup

        setup.run(args.setup or None)
        return
    cfg = config.load()
    notify_mod.configure(cfg)
    if args.serve:
        worker.serve()
    elif args.stop_worker:
        print("stopped" if worker.stop() else "no worker running")
    elif args.cancel:
        cancel()
    elif args.download:
        engines.download_all(cfg)
    elif args.compare:
        compare(cfg, Path(args.compare))
    elif args.info:
        show_info(cfg)
    elif recorder.is_recording():
        stop_and_transcribe(cfg)
    else:
        start(cfg)
