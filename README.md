# CDCT — Call Data & Conversation Transcripts

CDCT is a local Windows app that captures audio and writes a live,
timestamped markdown transcript. It works with calls, videos, and other
desktop audio; it is not tied to Discord, Zoom, or any single service.

Audio and transcription stay on your PC. The only network activity is the
one-time download of a Whisper model when you choose a model size that is not
already installed.

## Capture modes

CDCT has two explicit capture modes:

- **A selected program** — choose an open program under the Settings gear's
  **Capturing for** dropdown. CDCT uses Windows WASAPI process loopback to
  capture that process and its child processes only. This is the right choice
  for a Discord call, browser video, or media player when other apps are also
  making sound.
- **Full system output** — leave the default target unchanged, or explicitly
  choose to capture everything if CDCT cannot isolate the selected program.

CDCT never silently changes an attempted per-program capture into a
full-system recording. If isolation cannot start, it explains why and offers
**Cancel** or **Capture Everything Instead**.

The target list contains normal user-facing apps only. CDCT's own windows and
Windows system shell/settings windows are excluded.

### Important scope

Isolation is by **process**, not individual window or speaker. Two windows of
the same browser can share a process tree and therefore cannot be separated
from each other. CDCT also does not identify who spoke; it transcribes the
audio that the selected process produces.

## How it works

1. **Capture** — `capture.py` records either the selected app's process tree
   or the default output-device mix into rolling WAV chunks.
2. **Transcribe** — `transcriber.py` sends finished chunks to
   [faster-whisper](https://github.com/SYSTRAN/faster-whisper). CUDA is used
   when available; otherwise CDCT uses CPU automatically.
3. **Output** — timestamped segments are appended live to a markdown file.

## Using CDCT

- **Standalone app:** download `CDCT.exe` from the
  [Releases](../../releases) page. No Python installation is needed.
- **First run:** choose where transcripts and optional audio chunks should be
  stored. CDCT remembers that location.
- **Settings:** use the gear in the sidebar to choose the Whisper model,
  chunk length, capture target, and whether to retain WAV chunks after
  transcription.

The sidebar lists previous sessions. You can search an open transcript,
rename or pin sessions, and delete sessions you no longer need.

## Responsible use

Transcribing a call has the same consent and platform-rule considerations as
recording it. Tell participants when transcription is active, follow the laws
that apply to everyone on the call, and protect transcript files like any
other sensitive recording. See [LEGAL_GUIDELINES.md](LEGAL_GUIDELINES.md) for
the project guidance.

## Requirements

- Windows 10 version 2004 or newer, or Windows 11
- Python 3.10+ when running from source

## Running from source

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe gui.py
```

For the command-line interface:

```powershell
.\venv\Scripts\python.exe main.py --target-process Discord.exe
```

`--target-process` isolates the first matching running process. If isolation
cannot be initialized, the CLI prints a warning before using full system
output.

## Building the exe

```powershell
.\build_exe.ps1
```

This produces `dist\CDCT.exe` with the required native capture and
transcription dependencies bundled. GPU acceleration additionally requires
the relevant NVIDIA CUDA libraries in the build environment; CPU fallback is
automatic.

## Project files

- `capture.py` — full-system and per-process capture, plus target enumeration
- `transcriber.py` — Whisper worker and transcript writing
- `gui.py` — desktop UI, settings, session history, and first-run setup
- `main.py` — command-line entry point
- `build_exe.ps1` — PyInstaller build script

## License

[MIT](LICENSE)
