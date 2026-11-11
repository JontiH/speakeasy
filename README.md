# Speakeasy

Local voice dictation for Linux and Windows. Press a hotkey, talk, press it again, and
the text appears in the window you're typing in. Speech is transcribed on
your machine; there are no API keys and nothing is sent anywhere.

It was built for talking to terminal tools like [opencode](https://opencode.ai)
instead of typing, so by default it pastes into terminals and presses Enter
only when opencode is focused.

## Requirements

- Linux: Ubuntu 24.04 or similar, GNOME on Wayland. `install.sh` uses apt,
  and the Enter and paste behaviour relies on a small GNOME Shell extension.
  PipeWire or PulseAudio.
- Windows: Windows 10 or 11. The installer gets Python 3.12 and ffmpeg
  through winget if they're missing. No admin rights needed.
- About 1.4 GB of disk for the models, and 1.3 GB of RAM while the model is
  loaded. Everything runs on the CPU.

## Install

```bash
git clone https://github.com/JontiH/speakeasy.git ~/speakeasy
cd ~/speakeasy
./install.sh
```

`install.sh` asks for sudo, then:

- installs ffmpeg, wl-clipboard, ydotool and a few other packages,
- gives the `input` group access to `/dev/uinput` and adds you to it, so
  ydotool can type,
- builds a Python venv and downloads the speech models,
- starts `ydotoold` as a user service,
- installs the focused-window GNOME extension,
- runs `./speakeasy --setup`, which asks you to talk for a few seconds to
  find the mic that hears you, and binds the hotkeys.

Log out and back in afterwards so the group change and the extension take
effect. Re-running `install.sh` is safe.

If you keep personal settings (vocabulary, word fixes) in a config file, pass
it with `./install.sh --from <path or URL>` to install it on a new machine.

### Windows

```powershell
git clone https://github.com/JontiH/speakeasy.git $HOME\speakeasy
cd $HOME\speakeasy
.\install.cmd
```

`install.cmd` runs `install.ps1` (no need to change PowerShell's execution
policy), which:

- installs Python 3.12 and ffmpeg with winget, unless you have them,
- builds a Python venv and downloads the speech models,
- runs `.\speakeasy.cmd --setup`, which finds your mic, adds Speakeasy to
  your Startup folder and starts it.

Windows has no built-in way to run a command from a hotkey, so Speakeasy
runs a small tray icon (`speakeasy.cmd --listen`, about 30 MB) that owns the
hotkeys. It's a dot in the notification area, maybe behind the ^ arrow:
grey when idle, red while recording, amber while transcribing. Click it for
a menu. Set `output.sounds = true` for a soft chime when recording starts
and stops. Errors pop up as notifications.

Use `-From <path or URL>` with `install.cmd` to bring a personal config.

## Use

| Hotkey | Does |
|---|---|
| Super + / | Start recording. Press again to stop and insert the text. |
| Super + ' | Stop recording and throw the audio away. |

From a terminal:

| Command | Does |
|---|---|
| `./speakeasy` | Same as Super + /. |
| `./speakeasy --cancel` | Same as Super + '. |
| `./speakeasy --setup` | Pick the mic again and rebind the hotkeys. |
| `./speakeasy -v` | Show the engine, model, mic and what's running. |
| `./speakeasy --compare [file.wav]` | Run the last recording (or a WAV) through every engine, with timings. |
| `./speakeasy --stop-worker` | Unload the model now instead of after the idle timeout. |

On Windows, Super is the Windows key, and the command is `.\speakeasy.cmd`
with the same options, plus `--listen` (run the tray icon) and
`--stop-listener` (quit it).

## Configuration

[`config.toml`](config.toml) holds the defaults, with a comment on every
setting. On Windows, [`config.windows.toml`](config.windows.toml) is merged
over it (DirectShow mic, Ctrl+V, Windows app names). Put your own changes in
`~/.config/speakeasy/config.toml` (Windows: `%APPDATA%\speakeasy\config.toml`,
also under "Open personal config" in the tray menu) instead of editing them. Values there override the defaults: tables such as
`[replacements]` are merged, and any other value replaces the default.
`./speakeasy --setup` creates that file and writes your mic to it.

Things you're likely to change:

- `engine`: `parakeet` (default) or `whisper`.
- `[replacements]`: words the model keeps getting wrong, and how to fix them.
- `[cleanup] fillers`: words to drop, like "um" and "uh".
- `[output]`: where to press Enter, where to paste instead of type.
- `[hotkeys]`: re-run `./speakeasy --setup` after changing them.

## How it works

- **Recording:** ffmpeg records the mic to a WAV file. Recording is a toggle
  because GNOME on Wayland doesn't pass key releases to shortcuts, so
  hold-to-talk isn't possible.
- **Transcription:** the default engine is NVIDIA's Parakeet
  (`parakeet-tdt-0.6b-v2`, int8) through
  [onnx-asr](https://github.com/istupakov/onnx-asr). It's English only. The
  alternative is OpenAI's Whisper through
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper), which is
  slower but takes a vocabulary list.
- **Speed:** the first press starts a background worker that loads the model
  while you talk and keeps it loaded for 10 minutes after your last
  dictation. With Parakeet, long recordings are transcribed in chunks while
  you're still talking, so stopping only waits for the last few seconds. On
  a Ryzen 7 7840U, text appears about half a second after you stop for short
  dictations, and under a second for a minute or more.
- **Output:** the text is always copied to the clipboard. In terminals it's
  pasted with Ctrl+Shift+V; elsewhere it's typed with ydotool. Enter is
  pressed only in windows listed in `output.enter_only_in`.
- **Windows:** the same flow and engines, with Windows APIs in place of
  the Linux tools. ffmpeg records through DirectShow and is stopped with
  Ctrl+C sent to its hidden console, so it closes the WAV properly. The
  tray listener registers the hotkeys. Text is pasted with Ctrl+V in
  Windows Terminal, consoles, VS Code and the opencode desktop app, and
  typed as Unicode key events elsewhere. Window titles and program names
  (like `windowsterminal.exe`) decide where Enter is pressed. Windows
  Python has no Unix sockets, so the worker listens on a random localhost
  port and checks a per-run token. Windows blocks typing into apps running
  as administrator unless Speakeasy runs as administrator too.

## Development

```bash
./venv/bin/python -m unittest
# Windows: .\venv\Scripts\python.exe -m unittest
```

The worker keeps running the code it started with, so run
`./speakeasy --stop-worker` after changing anything in `src/`.
