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
- Windows: `src/speakeasy/win32.py` (ctypes: processes, Ctrl+C to ffmpeg,
  clipboard, SendInput, focused window, file locks), `winlisten.py` (tray
  icon + RegisterHotKey, `--listen`), `config.windows.toml` (merged over
  `config.toml` on Windows), `speakeasy.cmd`, `install.cmd`/`install.ps1`.

## Windows

- Paths: personal config `%APPDATA%\speakeasy\config.toml`, state dir
  `%TEMP%\speakeasy` (timing.log, listener.log, worker.log). The
  `XDG_*` overrides still win, which is how tests isolate themselves.
- Hotkeys: `speakeasy --listen` (pythonw, started from
  `Startup\Speakeasy.lnk`, made by `--setup`) registers them and runs the
  cli flows in a thread. `--setup` test-registers each hotkey first and
  reports clashes. `venv\Lib\site-packages\speakeasy.pth` puts `src/` on
  the path so the shortcut needs no PYTHONPATH.
- Never call `os.kill(pid, 0)` on Windows: it terminates the process. Use
  `win32.pid_alive`, which also checks the exe is ffmpeg (PID reuse).
- ffmpeg runs with `CREATE_NO_WINDOW`; stop sends Ctrl+C from a helper
  process that attaches to its console (doing it in-process would interrupt
  the caller). If that fails it's killed and `recorder.fix_wav_header`
  repairs the sizes. dshow uses `-audio_buffer_size 50`.
- Worker IPC: localhost TCP; `worker.addr` holds `<port> <token>`.
- Notifications: tray tooltip always; pop-ups only for errors unless
  `notifications = "normal"`. `output.sounds` beeps on start/stop.
- Tests must not inject input: `test_output.py` patches `output.WINDOWS`
  and fakes `win32`. Running the Linux tests unpatched on Windows typed into
  the focused window.

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
- A long-running ydotoold can hang: it accepts clients but types nothing, and
  every `ydotool` call blocks. Calls have a timeout (`output.ydotool_timeout_s`);
  on expiry `output.restart_ydotoold` restarts the user service and retries
  paste/Enter once. The timing log shows `ydotool timed out` and
  `restarting ydotoold`.

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

## Debugging a dictation that didn't send

Symptom: the text appears in the input but Enter isn't pressed (or the text
lands somewhere odd). It happens in any session, so run this from wherever the
owner is. Read-only: don't toggle, cancel or stop the worker (see "Testing
without disturbing the owner").

1. Read the tail of `$XDG_RUNTIME_DIR/speakeasy/timing.log`. The last
   `focused window:` line starts the failed run. A full run logs `copied`,
   `focused window`, `pasted`/`typed` (plus `FAILED`), then `enter sent`,
   `enter FAILED`, `enter skipped: ...` or `enter off`, and `focused window
   after Enter`. Compare the two window lines to spot a focus change.
2. Compare that window with the config: paste needs `app_id`/`wm_class` to
   match `output.paste_in`, Enter needs `title`/`wm_class`/`app_id` to match
   `output.enter_only_in`. `typed` in a terminal means the window was misread.
3. Known cause: `wl-copy` opens a `wl-clipboard` surface that briefly holds
   focus, and `emit()` can read that instead of the terminal. Signature:
   `focused window: {'wm_class': '', 'title': 'wl-clipboard', ...}` then
   `typed`. Count them with `grep -c "'title': 'wl-clipboard'" timing.log`.
   `emit()` re-reads the window up to 5 times, 50 ms apart, logging
   `focus still on wl-clipboard, re-reading (n/5)`. A failed run with all
   5 retries means the surface held focus longer than that.
4. Rule out the rest: `ydotool timed out` or `restarting ydotoold` lines,
   `systemctl --user status ydotoold`, a `not sent` notification (the title
   didn't match), and Enter racing a long paste (`enter sent` with the text
   not yet in the input, or the paste dropped). The wait before Enter grows
   with the text (`paste_enter_delay_ms` plus `paste_enter_delay_per_char_ms`
   per character) and is logged as `waiting N ms before Enter`.
5. Report the failed run's log line, which cause it matches, and whether
   `output.py` already handles it. Fix only if asked. `output.py` runs in the
   hotkey process, so an edit there is live on the next dictation and needs no
   `--stop-worker`.

## Fixing misheard words

When the owner says something like "it wrote cube CTL, it should be
kubectl", add the heard form to `[replacements]` in
`~/.config/speakeasy/config.toml`, never the repo's `config.toml`:

- Key is the correct text, value is a list of heard forms. If the key
  exists, append to its list; don't replace it.
- Matching ignores case and treats spaces, hyphens, dots and slashes as the
  same, so one entry covers "cube CTL" and "Cube-CTL". Skip variants
  that only differ that way.
- Keep heard forms specific. Matching is whole-word, but a short heard
  form like "cp" also gets replaced where it was meant literally.
- Check the file still parses (`./speakeasy -v`). It's read on every
  dictation, so there's nothing to restart.

Only generic terms (tool names anyone might say) belong in the repo's
`config.toml`; work and personal terms stay in the personal file.

## Next steps

- After the owner's next login, remove `focused-window@whisper-local` from
  `org.gnome.shell enabled-extensions`. It's the extension's pre-rename ID;
  the files are gone and `focused-window@speakeasy` replaces it. Removing it
  before then unloads the running copy and Enter stops working until login.
- Choose a licence (MIT suggested). The repo is public without one.
- Optional `./speakeasy --fix "<heard>" "<correct>"` to add a replacement
  without an agent. Only if the routine above turns out to be a chore.
- Matching that also ignores spacing inside words, so "cubectl" covers
  "cube CTL" and "cube C T L". Risk: short entries match across word
  boundaries.
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
