# CDCT — Call Data & Conversation Transcripts

A local, general-purpose voice-to-text dictation tool for Windows. It
captures your PC's audio output and transcribes it live to a markdown file
— useful for call notes, meeting transcripts, dictating from a video, or
just capturing your own voice. It isn't tied to Discord, Zoom, or any other
single application.

Everything runs locally on your machine. Audio is never uploaded anywhere;
the only network request is a one-time model download from Hugging Face the
first time you pick a given model size.

## How it works

1. **Capture** — `capture.py` opens a WASAPI loopback stream on your
   default audio output device (the same mechanism OBS uses for
   "Application Audio Capture") and records it in rolling chunks (30
   seconds by default), saved as WAV files. A dropped/interrupted audio
   stream is detected and automatically reconnected rather than silently
   killing the recording.
2. **Transcribe** — `transcriber.py` feeds each finished chunk to
   [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (a fast
   CTranslate2-based reimplementation of OpenAI's Whisper) as soon as it's
   ready, rather than waiting for the whole session to end. GPU (CUDA) is
   used automatically if available, falling back to CPU otherwise.
3. **Output** — Each transcribed segment is appended to a markdown file
   with a wall-clock timestamp, live, while the session is still running.

```
Audio output device (WASAPI loopback)
        │
        ▼
Rolling WAV chunks
        │
        ▼
faster-whisper transcription (GPU or CPU)
        │
        ▼
Timestamped markdown transcript, updated live
```

**Important:** this captures your **entire output device**, not a single
application's audio. True per-process capture on Windows requires a COM
async activation API with no mature Python wrapper (see the note at the top
of `capture.py`). In practice this means: close or mute anything else
making sound before you start, or its audio will end up in the transcript
too.

## Using it

- **Standalone app**: download `CDCT.exe` from the
  [Releases](../../releases) page — no Python install needed. First launch
  asks where to store your transcripts (a default location, Documents,
  Desktop, or a folder you choose), then remembers that choice.
- **From source**: see [Running from source](#running-from-source) below.

The window has:
- A **sidebar** listing past sessions — right-click one to rename, pin to
  the top, or delete it. Click the title at the top of an open session to
  rename it too.
- A **search box** to filter the currently open transcript.
- A **settings menu** (gear icon, bottom-left of the sidebar) for the
  Whisper model size, chunk length, which open program to check for before
  recording starts, and whether to keep the raw audio chunks afterward.

## Legal and responsible use

Capturing and transcribing live audio is subject to the same legal
frameworks as audio recording generally — this isn't a grey area just
because the output is text instead of an audio file. Full guidance is in
[`LEGAL_GUIDELINES.md`](LEGAL_GUIDELINES.md); the short version:

- **Consent laws vary by jurisdiction.** Some places only require your own
  consent to record a call you're part of; others require everyone on the
  call to agree. When participants are in different states or countries,
  the strictest applicable law usually wins.
- **Say something.** Announcing that you're using a transcription tool
  when you join a call is the simplest way to stay on the right side of
  consent requirements almost everywhere.
- **Respect platform terms of service.** Some platforms prohibit
  unauthorized capture of call audio, particularly where it's end-to-end
  encrypted.
- **Handle the output responsibly.** A transcript can contain the same
  sensitive information as the call itself — trade secrets, personal
  information, anything covered by an NDA. Treat the markdown file with
  the same care you'd give a recording.

CDCT already follows the best-practice recommendation to prefer local
processing: nothing you capture is sent to a cloud service or used to
train anything. That doesn't change the consent/disclosure obligations
above, which depend on how *you* use the tool, not on how it processes
audio.

## Known limitations

- Captures the full output device mix — no per-speaker separation, and
  other apps' audio bleeds in if they're making sound too.
- Not a live captions overlay — check the markdown file during or after
  the session, not word-by-word as it's spoken.
- Model accuracy/speed is a real tradeoff — see the in-app tooltip on the
  Model setting.

## Running from source

Requires Python 3.10+ and Windows 10 2004+ (for WASAPI loopback).

```
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe gui.py      # GUI
venv\Scripts\python.exe main.py     # CLI
```

CLI options: `--chunk-seconds`, `--model`, `--keep-audio`,
`--target-process` (an informational presence check only — it doesn't
change what's captured).

### Building the exe

```
.\build_exe.ps1
```

Produces `dist\CDCT.exe` via PyInstaller. GPU support needs
`nvidia-cublas-cu12` and `nvidia-cudnn-cu12` installed in the venv;
without them it runs on CPU automatically.

## Files

- `capture.py` — WASAPI loopback recording, chunked to WAV, window/process
  enumeration for the "capturing for" picker
- `transcriber.py` — faster-whisper transcription worker, appends to
  markdown
- `main.py` — CLI entry point
- `gui.py` — GUI entry point (customtkinter): sidebar history, settings,
  search, rename/pin/delete
- `build_exe.ps1` — builds `dist\CDCT.exe` via PyInstaller

## License

[MIT](LICENSE)
