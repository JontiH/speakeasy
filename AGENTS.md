# AGENTS.md: Speakeasy

Local voice dictation for talking to CLI tools (mainly opencode) instead of
typing. Press a hotkey, talk, press it again, and the text appears in the
focused window. No API keys, no cloud.

The goal is that `git clone` plus `./install.sh` on another Linux laptop gives
the same setup the owner has here.

## Git identity

Commit with the personal identity in this repo's **local** git config
(`JontiH`, plus a `core.sshcommand` for the personal SSH key). The global git
config is a work identity; never commit or push with it, and never change the
local config.

`gh` has both accounts; the active one must stay the work account, because
every other repo on this machine is work. Never run `gh auth switch`. For `gh` in
this repo, pass the personal token per command:
`GH_TOKEN=$(gh auth token --user JontiH) gh repo view JontiH/speakeasy`.
Pushes go over SSH with the personal key from `core.sshcommand`.

## Development machine

Ubuntu 24.04, GNOME on Wayland, PipeWire (use `wpctl`/`pw-dump`; there's no
`pactl`). AMD Ryzen 7 PRO 7840U with a Radeon 780M and no NVIDIA GPU, so
everything runs on the CPU.

## Layout

- `speakeasy`: bash launcher the hotkeys run; runs `python -m speakeasy` with
  `src/` on `PYTHONPATH`.
- `src/speakeasy/`
  - `cli.py`: arguments, and the start / stop / cancel flows.
  - `config.py`: paths, loading `config.toml` plus the personal override,
    and `mark()` for the timing log.
  - `recorder.py`: ffmpeg capture.
  - `engines.py`: Parakeet and Whisper, model cache, `MODEL_LOCK`.
  - `streaming.py`: chunked transcription during recording.
  - `worker.py`: the background process that keeps the model loaded.
  - `text.py`: replacements and filler removal.
  - `output.py`: clipboard, ydotool typing/paste, Enter gating.
  - `notify.py`: notifications.
  - `setup.py`: `--setup`: personal config, mic detection, hotkeys.
- `config.toml`: defaults. These are the owner's preferences, minus anything
  private or machine-specific.
- `gnome-extension/`: the focused-window extension.
- `systemd/ydotoold.service`: user unit installed by `install.sh`.
- `install.sh`: apt packages, uinput access, venv, models, ydotoold,
  extension, then `speakeasy --setup`. Safe to re-run.
- `tests/`: `./venv/bin/python -m unittest`. Tests redirect
  `XDG_RUNTIME_DIR` and `XDG_CONFIG_HOME` to temp dirs.

## Config

`config.toml` in the repo holds defaults. `~/.config/speakeasy/config.toml`
overrides it: tables merge key by key, lists and other values replace. The
personal file holds the mic, work vocabulary and work replacements, and is
never committed. `speakeasy --setup [path|URL]` creates it, optionally copies
one in first, detects the mic and binds hotkeys. Machine-specific values
(the mic) shouldn't go in copies carried between computers.

## How it works

- **Toggle, not hold-to-talk:** GNOME on Wayland can't deliver key-up events
  to a shortcut. Hotkeys: Super+/ toggles, Super+' cancels.
- **Capture:** ffmpeg `-f pulse` to `$XDG_RUNTIME_DIR/speakeasy/record.wav`,
  with `-flush_packets 1` so the file grows in real time. PortAudio was
  dropped because it needed an extra system library.
- **Engines:** `parakeet` (default): `nemo-parakeet-tdt-0.6b-v2` int8 through
  onnx-asr. It takes at most ~20-30 s per call and has no vocabulary support.
  `whisper`: faster-whisper, loaded with `local_files_only` so it doesn't
  contact huggingface.co each run; supports a `vocabulary` prompt.
  `[replacements]` and filler removal apply to both.
- **Worker:** the first press spawns `speakeasy --serve`, which loads the
  model while you talk and answers on `worker.sock`. It exits after
  `keep_loaded_minutes` without a request, never while recording. Parakeet
  holds ~1.3 GB. If the worker is missing or fails, the hotkey process
  transcribes itself. **A running worker keeps old code: after changing
  Python files, run `./speakeasy --stop-worker`.**
- **Streaming (Parakeet):** the worker reads new bytes from `record.wav`
  every 0.5 s. With 18 s pending, it cuts at the quietest 300 ms between 9
  and 18 s and transcribes that chunk, so stop only waits for the tail.
  Measured: ~0.5 s from stop to text for short clips, ~0.9 s for 30-80 s.
  Silero VAD is only used for long recordings when streaming didn't run (the
  onnx-asr defaults split at every 100 ms gap and produced broken sentences
  and a hallucinated "Mm-hmm").
- **Output:** always copies to the clipboard. In apps matching
  `output.paste_in` it pastes with Ctrl+Shift+V; elsewhere it types with
  ydotool. Enter only when the focused window matches
  `output.enter_only_in`, read from the `focused-window@speakeasy`
  extension over D-Bus (`org.gnome.Shell.FocusedWindow.Get`). opencode runs
  in gnome-terminal and titles it `OC | <session>`, so the match comes from
  the title; a plain bash tab must not match. New extensions only load after
  re-login on Wayland.
- **Notifications:** one bubble updated in place via `--replace-id` (ID in
  `notify.id`). GNOME doesn't always honour `--transient`, so the final
  message is closed with `CloseNotification` 4 s later, unless a newer run
  has rewritten `notify.id`. Errors persist.
- **Timing log:** `$XDG_RUNTIME_DIR/speakeasy/timing.log`, one line per
  step, worker lines prefixed `worker:`. Check it first when something is
  slow or falls back.

## ydotool on Ubuntu (0.1.8, not 1.x)

- `wtype` doesn't work: Mutter has no virtual-keyboard protocol.
- `ydotoold` is a separate apt package, takes no flags and always listens on
  `/tmp/.ydotool_socket` (1.x flags like `--socket-path` don't exist).
  `YDOTOOL_SOCKET` overrides the path for 1.x.
- Without the daemon, each call makes a new uinput device and GNOME drops the
  first keys, so `--delay 400` is added when the socket is missing.
- Text goes on stdin (`--file -`) so a leading `-` isn't read as a flag.
- `ydotool key` exits 0 on unknown key names, so a typo fails silently.

## Testing without disturbing the owner

The owner dictates into this session with the live hotkey, which runs the
main checkout. Don't run `./speakeasy` with no arguments, or `--cancel`, against
the real state dir: it stops or discards their recording. Check
`$XDG_RUNTIME_DIR/speakeasy/record.pid` first, and for end-to-end tests
point `XDG_RUNTIME_DIR` and `XDG_CONFIG_HOME` at temp dirs, set
`output.mode = "stdout"`, and feed a WAV with
`ffmpeg -re -i clip.wav -ac 1 -ar 16000 -flush_packets 1 $D/record.wav`
while writing its PID to `record.pid`. PipeWire needs the real
`XDG_RUNTIME_DIR`, so mic tests (`--setup`) only redirect the config dir.
`--setup` also rebinds the GNOME hotkeys to the checkout it runs from;
clear `XDG_CURRENT_DESKTOP` to skip that.

## Possible improvements

- A transcript log plus a routine for turning recurring mishearings into
  `[replacements]`, and matching that ignores spacing inside words so one
  entry covers "Mono CI C D" and "mono CICD".
- Background audio from the laptop's own speakers gets transcribed during
  pauses ("90 degree bend" from a stream came out as "90 degree bed"). Fix:
  PipeWire `libpipewire-module-echo-cancel` with `libspa-aec-webrtc` (both
  installed), a config in `~/.config/pipewire/pipewire.conf.d/`, and
  `audio.device` pointed at the Echo-Cancel Source. Needs a PipeWire restart,
  so ask first.
- GPU: whisper.cpp built with Vulkan could use the Radeon 780M. Skipped for
  now: after streaming, transcription is only ~0.2-0.6 s of the wait, and it
  needs Vulkan dev packages (sudo) plus a third backend.
- Two hotkeys instead of `press_enter`: one types only, one types and sends.
